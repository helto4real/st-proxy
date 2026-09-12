from __future__ import annotations

import asyncio
import json

from aiohttp import web

from .support import MockKobold


class MockTabby(MockKobold):
    def __init__(self) -> None:
        super().__init__()
        self.model = "synthetic-exl3"
        self.parameters = {
            "max_seq_len": 32768, "cache_size": 65536,
            "cache_mode": "Q4", "chunk_size": 2048, "use_vision": False,
        }
        self.loads: list[dict] = []
        self.unloads = 0
        self.load_error = False
        self.load_finished = asyncio.Event()
        self.load_release = asyncio.Event()
        self.hold_load = False
        self.wrong_context = False
        self.payloads: list[bytes] = []

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/health", self.health)
        app.router.add_get("/v1/model", self.current)
        app.router.add_get("/v1/models", self.get_models)
        app.router.add_get("/v1/model/list", self.get_models)
        app.router.add_post("/v1/model/load", self.load)
        app.router.add_post("/v1/model/unload", self.unload)
        app.router.add_post("/v1/chat/completions", self.chat)
        return app

    async def health(self, request):
        self.request_paths.append(request.path)
        return web.json_response({"status": "healthy", "issues": []})

    async def current(self, request):
        self.request_paths.append(request.path)
        if self.model == "inactive":
            return web.json_response({"detail": "No models are currently loaded."}, status=503)
        return web.json_response({"id": self.model, "parameters": self.parameters})

    async def load(self, request):
        payload = await request.json()
        self.loads.append(payload)
        self.events.append("load")
        self.model = "inactive"
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        # Split an SSE line across writes; component completion precedes readiness.
        await response.write(b': ping\r\n\r\nda')
        await response.write(
            b'ta: {"model_type":"model","status":"finished","module":1,"modules":1}\r\n\r\n'
        )
        self.load_finished.set()
        if self.hold_load:
            await self.load_release.wait()
        if self.load_error:
            await response.write(b'data: {"error":{"message":"synthetic failure"}}\n\n')
        else:
            self.model = payload["model_name"]
            self.parameters.update({
                key: payload[key] for key in self.parameters if key in payload
            })
            if self.wrong_context:
                self.parameters["max_seq_len"] = 4096
        try:
            await response.write_eof()
        except ConnectionError:
            pass
        return response

    async def unload(self, request):
        self.unloads += 1
        self.events.append("unload")
        if self.unload_failures:
            self.unload_failures -= 1
            return web.json_response({"detail": "synthetic failure"}, status=500)
        self.model = "inactive"
        return web.json_response(None)

    async def chat(self, request):
        raw = await request.read()
        self.payloads.append(raw)
        payload = json.loads(raw)
        if payload.get("stream"):
            return await self.stream(request)
        self.chat_requests += 1
        self.events.append("chat")
        return web.json_response({"choices": [{"message": {
            "content": "synthetic", "reasoning_content": "synthetic reasoning",
        }}]})
