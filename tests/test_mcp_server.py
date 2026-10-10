from __future__ import annotations

import asyncio
import socket
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from st_proxy.service import BrokerService

from . import test_media_jobs as fixtures
from .support import wait_until


class MCPTransportTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.MediaJobsTests.asyncSetUp

    @property
    def url(self):
        return f"http://127.0.0.1:{self.service.mcp_listener.port}/mcp"

    @asynccontextmanager
    async def sdk_client(self, token="synthetic-mcp-token"):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with httpx2.AsyncClient(headers=headers, trust_env=False) as http:
            async with streamable_http_client(self.url, http_client=http) as streams:
                async with ClientSession(*streams) as client:
                    await client.initialize()
                    yield client

    async def test_official_client_discovery_generate_and_get_job(self):
        async with self.sdk_client() as client:
            tools = await client.list_tools()
            self.assertEqual({t.name for t in tools.tools},
                             {"list_workflows", "describe_workflow", "generate",
                              "generate_batch", "get_job", "register_image"})
            listing = await client.call_tool("list_workflows", {})
            self.assertFalse(listing.is_error)
            self.assertEqual(listing.structured_content["workflows"][0]["id"], "example")
            description = await client.call_tool("describe_workflow", {"workflow_id": "example"})
            self.assertIn("prompt", description.structured_content["parameters"]["properties"])
            self.assertFalse(self.llm.admin_calls)
            self.assertFalse(self.comfy.prompt_calls)
            result = await client.call_tool("generate", {
                "job_id": "sdk-job", "workflow_id": "example", "parameters": {"prompt": "test"},
            })
            self.assertFalse(result.is_error, result)
            self.assertEqual(result.structured_content["status"], "succeeded")
            recovered = await client.call_tool("get_job", {"job_id": "sdk-job"})
            self.assertEqual(recovered.structured_content, result.structured_content)
            failure = await client.call_tool("generate", {
                "job_id": "sdk-job", "workflow_id": "example", "parameters": {"prompt": "other"},
            })
            self.assertTrue(failure.is_error)
            self.assertEqual(failure.structured_content["error"]["category"], "conflict")
            self.assertEqual(self.llm.admin_calls, ["unload_model"])

    async def test_batch_discovery_generation_and_recovery(self):
        async with self.sdk_client() as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            batch = tools["generate_batch"]
            self.assertIn("Prefer this tool", batch.description)
            self.assertEqual(batch.input_schema["properties"]["items"]["type"], "array")
            self.assertIn("generate_batch", tools["generate"].description)
            result = await client.call_tool("generate_batch", {
                "job_id": "batch", "workflow_id": "example",
                "items": [{"prompt": "forest"}, {"prompt": "lake", "seed": 2}],
            })
            self.assertFalse(result.is_error, result)
            self.assertEqual(result.structured_content["status"], "succeeded")
            self.assertEqual(len(result.structured_content["items"]), 2)
            recovered = await client.call_tool("get_job", {"job_id": "batch"})
            self.assertEqual(recovered.structured_content, result.structured_content)
            self.assertEqual(self.llm.admin_calls, ["unload_model"])

    async def test_http_auth_origin_and_host_checks(self):
        async with self.client.post(self.url, json={}) as response:
            self.assertEqual(response.status, 401)
        async with self.client.post(self.url, json={}, headers={
            "Authorization": "Bearer wrong-token",
        }) as response:
            self.assertEqual(response.status, 401)
        headers = {"Authorization": "Bearer synthetic-mcp-token",
                   "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        async with self.client.post(self.url, json={}, headers={
            **headers, "Origin": "https://untrusted.invalid",
        }) as response:
            self.assertEqual(response.status, 403)
        async with self.client.post(self.url, json={}, headers={
            **headers, "Host": "untrusted.invalid",
        }) as response:
            self.assertEqual(response.status, 421)
        self.assertFalse(self.llm.admin_calls)

    async def test_without_token_keeps_loopback_origin_and_host_checks(self):
        await self.service.stop()
        self.service = BrokerService(replace(self.config, mcp_token=None))
        self.addAsyncCleanup(self.service.stop)
        await self.service.start()
        self.assertEqual(self.service.mcp_listener.socket.getsockname()[0], "127.0.0.1")
        async with self.sdk_client(token=None) as client:
            listing = await client.call_tool("list_workflows", {})
            self.assertFalse(listing.is_error)
            self.assertEqual(listing.structured_content["workflows"][0]["id"], "example")
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        async with self.client.post(self.url, json={}, headers={
            **headers, "Origin": "https://untrusted.invalid",
        }) as response:
            self.assertEqual(response.status, 403)
        async with self.client.post(self.url, json={}, headers={
            **headers, "Host": "untrusted.invalid",
        }) as response:
            self.assertEqual(response.status, 421)
        self.assertFalse(self.llm.admin_calls)
        self.assertFalse(self.comfy.prompt_calls)

    async def test_client_disconnect_recovers_over_new_session(self):
        self.comfy.auto_complete = False
        async with self.sdk_client() as client:
            call = asyncio.create_task(client.call_tool("generate", {
                "job_id": "lost-call", "workflow_id": "example", "parameters": {"prompt": "x"},
            }))
            await wait_until(lambda: bool(self.comfy.prompt_calls))
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)
        self.comfy.completion[self.comfy.prompt_calls[0]].set()
        async with self.sdk_client() as client:
            result = await client.call_tool("get_job", {"job_id": "lost-call"})
            self.assertFalse(result.is_error, result)
            self.assertEqual(result.structured_content["status"], "succeeded")
        self.assertEqual(len(self.comfy.prompt_calls), 1)

    async def test_occupied_port_closes_failed_listener(self):
        from st_proxy.mcp_server import McpListener
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            listener = McpListener(self.jobs, occupied.getsockname()[1], "token")
            with self.assertRaises(OSError):
                await listener.start()
            self.assertIsNone(listener.socket)
            self.assertIsNone(listener.task)
        self.assertFalse(self.llm.admin_calls)
