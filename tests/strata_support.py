from __future__ import annotations

import asyncio

from aiohttp import web


class MockStrata:
    def __init__(self, model="synthetic-strata"):
        self.model = model
        self.loaded = False
        self.operations = []
        self.payloads = []
        self.fail_action = None
        self.false_confirmation = False
        self.confirm = asyncio.Event()
        self.confirm.set()
        self.hold_chat = False
        self.chat_started = asyncio.Event()
        self.chat_release = asyncio.Event()

    def app(self):
        app = web.Application()
        app.router.add_get('/health', self.health)
        app.router.add_get('/v1/status', self.health)
        app.router.add_get('/v1/models', self.models)
        app.router.add_get('/models', self.models)
        app.router.add_post('/v1/load', self.change)
        app.router.add_post('/v1/unload', self.change)
        app.router.add_post('/v1/chat/completions', self.chat)
        app.router.add_post('/v1/messages', self.chat)
        app.router.add_post('/v1/messages/count_tokens', self.chat)
        return app

    async def health(self, request):
        return web.json_response({'service': 'strata', 'status': 'ok',
                                  'model': self.model, 'loaded': self.loaded})

    async def models(self, request):
        return web.json_response({'object': 'list', 'data': [{
            'id': self.model, 'object': 'model',
            'status': {'value': 'loaded' if self.loaded else 'unloaded'},
            'architecture': {'input_modalities': ['text']},
        }]})

    async def change(self, request):
        payload = await request.json()
        if payload.get('model') != self.model:
            return web.json_response({'error': 'wrong model'}, status=404)
        action = request.path.rsplit('/', 1)[-1]
        self.operations.append(action)
        if self.fail_action == action:
            return web.json_response({'error': 'synthetic failure'}, status=503)
        await self.confirm.wait()
        if not self.false_confirmation:
            self.loaded = action == 'load'
        return web.json_response({'status': 'loaded' if action == 'load' else 'unloaded'})

    async def chat(self, request):
        self.payloads.append(await request.json())
        assert self.loaded
        self.chat_started.set()
        if self.hold_chat:
            response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
            await response.prepare(request)
            await response.write(b'data: synthetic-one\n\n')
            await self.chat_release.wait()
            await response.write(b'data: synthetic-two\n\n')
            await response.write_eof()
            return response
        return web.json_response({'choices': [{'message': {'content': 'synthetic'}}]})
