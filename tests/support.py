from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web


async def wait_until(predicate, timeout_seconds: float = 2.0) -> None:
    async with asyncio.timeout(timeout_seconds):
        while not predicate():
            await asyncio.sleep(0.005)


class DynamicServer:
    def __init__(self, app: web.Application) -> None:
        self._runner = web.AppRunner(app, access_log=None)
        self._site: web.TCPSite | None = None
        self.url = ""

    async def start(self) -> str:
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        assert self._site._server is not None and self._site._server.sockets
        port = self._site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self.url

    async def close(self) -> None:
        await self._runner.cleanup()


@dataclass
class MockKobold:
    model: str = "synthetic-model.gguf"
    reload_model: str = "synthetic-model.gguf"
    admin_enabled: bool = True
    unload_failures: int = 0
    reload_failures: int = 0
    admin_calls: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    version_paths: list[str] = field(default_factory=list)
    chat_requests: int = 0
    chat_started: asyncio.Event = field(default_factory=asyncio.Event)
    chat_release: asyncio.Event = field(default_factory=asyncio.Event)
    hold_chat: bool = False
    admin_authorization: list[str | None] = field(default_factory=list)

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/api/admin/reload_config", self.admin)
        app.router.add_get("/api/admin/list_options", self.list_options)
        app.router.add_get("/api/v1/model", self.get_model)
        app.router.add_get("/api/v1/info/version", self.version)
        app.router.add_get("/api/extra/version", self.version)
        app.router.add_route("*", "/api/extra/generate/stream", self.stream)
        app.router.add_route("*", "/api/v1/generate", self.generate)
        app.router.add_route("*", "/{tail:.*}", self.generic)
        return app

    async def admin(self, request: web.Request) -> web.Response:
        payload = await request.json()
        filename = payload["filename"]
        self.admin_calls.append(filename)
        self.admin_authorization.append(request.headers.get("Authorization"))
        self.events.append(filename)
        if not self.admin_enabled:
            return web.json_response({"success": False})
        if filename == "unload_model":
            if self.unload_failures:
                self.unload_failures -= 1
                return web.json_response({"error": "synthetic unload failure"}, status=500)
            self.model = "inactive"
        elif filename == "initial_model":
            if self.reload_failures:
                self.reload_failures -= 1
                return web.json_response({"error": "synthetic reload failure"}, status=500)
            self.model = self.reload_model
        return web.json_response({"success": True})

    async def get_model(self, _request: web.Request) -> web.Response:
        return web.json_response({"result": self.model})

    async def version(self, request: web.Request) -> web.Response:
        self.version_paths.append(request.path)
        return web.json_response(
            {"version": "test-fixture", "admin": 1 if self.admin_enabled else 0}
        )

    async def list_options(self, _request: web.Request) -> web.Response:
        options = ["initial_model", "unload_model"] if self.admin_enabled else []
        return web.json_response(options)

    async def stream(self, _request: web.Request) -> web.StreamResponse:
        self.chat_requests += 1
        self.events.append("chat_started")
        self.chat_started.set()
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(_request)
        await response.write(b"data: synthetic-one\n\n")
        if self.hold_chat:
            await self.chat_release.wait()
        await response.write(b"data: synthetic-two\n\n")
        await response.write_eof()
        self.events.append("chat_finished")
        return response

    async def generate(self, _request: web.Request) -> web.Response:
        self.chat_requests += 1
        self.events.append("chat_generate")
        return web.json_response({"results": [{"text": "synthetic"}]})

    async def generic(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True})


@dataclass
class MockComfy:
    auto_complete: bool = True
    fail_next_job: bool = False
    cleanup_failures: int = 0
    prompt_calls: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    completion: dict[str, asyncio.Event] = field(default_factory=dict)
    failures: set[str] = field(default_factory=set)
    free_calls: int = 0
    interrupt_calls: int = 0

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/prompt", self.prompt)
        app.router.add_post("/api/prompt", self.prompt)
        app.router.add_get("/history/{prompt_id}", self.history)
        app.router.add_post("/free", self.free)
        app.router.add_post("/interrupt", self.interrupt)
        app.router.add_route("*", "/{tail:.*}", self.generic)
        return app

    async def prompt(self, request: web.Request) -> web.Response:
        await request.read()
        prompt_id = f"synthetic-{len(self.prompt_calls) + 1}"
        self.prompt_calls.append(prompt_id)
        self.events.append(f"submit:{prompt_id}")
        event = self.completion[prompt_id] = asyncio.Event()
        if self.fail_next_job:
            self.fail_next_job = False
            self.failures.add(prompt_id)
            event.set()
        elif self.auto_complete:
            event.set()
        return web.json_response({"prompt_id": prompt_id, "number": len(self.prompt_calls)})

    async def history(self, request: web.Request) -> web.Response:
        prompt_id = request.match_info["prompt_id"]
        event = self.completion.get(prompt_id)
        if event is None or not event.is_set():
            return web.json_response({})
        failed = prompt_id in self.failures
        return web.json_response(
            {
                prompt_id: {
                    "status": {
                        "completed": not failed,
                        "status_str": "error" if failed else "success",
                    },
                    "outputs": {} if failed else {"1": {"images": []}},
                }
            }
        )

    async def free(self, request: web.Request) -> web.Response:
        assert await request.json() == {"unload_models": True, "free_memory": True}
        self.free_calls += 1
        self.events.append("free")
        if self.cleanup_failures:
            self.cleanup_failures -= 1
            return web.json_response({"error": "synthetic cleanup failure"}, status=500)
        return web.json_response({"ok": True})

    async def interrupt(self, _request: web.Request) -> web.Response:
        self.interrupt_calls += 1
        self.events.append("interrupt")
        return web.json_response({"ok": True})

    async def generic(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True})


def prompt_payload() -> dict[str, Any]:
    return {
        "client_id": "synthetic-client",
        "prompt": {"1": {"class_type": "SyntheticNode", "inputs": {"value": 1}}},
    }
