import json
import unittest

from aiohttp import web

from st_proxy.llm.prism_diagnostics import MAX_EVENT_BYTES, PrismDiagnostics, request_usage

PRIVATE = 'PRIVATE prompt/model/answer å🙂'
LOGGER = 'st_proxy.llm.prism_diagnostics'


def stream_bytes():
    events = [
        {'choices': [{'delta': {'reasoning_content': PRIVATE}, 'finish_reason': None}]},
        {'choices': [{'delta': {'content': PRIVATE}, 'finish_reason': 'length'}]},
        {'choices': [], 'usage': {'prompt_tokens': 14268, 'completion_tokens': 1500,
                                 'prompt_tokens_details': {'cached_tokens': 12000}}},
    ]
    return b''.join(b'data: ' + json.dumps(e, ensure_ascii=False).encode() + b'\r\n\r\n'
                    for e in events) + b'data: [DONE]\r\n\r\n'


async def diagnostic_stream(request):
    response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
    await response.prepare(request)
    wire = stream_bytes()
    for offset in range(0, len(wire), 7):
        await response.write(wire[offset:offset + 7])
    await response.write_eof()
    return response


class PrismDiagnosticsTests(unittest.TestCase):
    def observer(self, **extra):
        return PrismDiagnostics(json.dumps({
            'model': PRIVATE, 'messages': [{'role': 'user', 'content': PRIVATE}],
            'max_tokens': 1500, 'reasoning_effort': 'none',
            'chat_template_kwargs': {'enable_thinking': False}, **extra,
        }).encode())

    def finish_log(self, observer):
        with self.assertLogs(LOGGER, 'INFO') as captured:
            observer.finish(upstream_complete=True, client_connected=True)
        self.assertEqual(len(captured.output), 1)
        self.assertNotIn(PRIVATE, captured.output[0])
        return captured.output[0]

    def test_fragmented_sse_counts_characters_and_usage_without_content(self):
        observer = self.observer()
        observer.start(200, 'text/event-stream')
        for byte in stream_bytes():
            observer.feed(bytes([byte]))
        log = self.finish_log(observer)
        for value in ('max_tokens=1500', 'reasoning_effort=none', 'enable_thinking=false',
                      'prompt_tokens=14268', 'cached_tokens=12000', 'completion_tokens=1500',
                      'reasoning_tokens=unavailable', f'reasoning_chars={len(PRIVATE)}',
                      'finish_reason=length', 'stream_status=complete'):
            self.assertIn(value, log)

    def test_json_off_and_missing_usage_are_not_invented(self):
        observer = self.observer()
        observer.start(200, 'application/json')
        observer.feed(json.dumps({'choices': [{'message': {'content': PRIVATE},
                                               'finish_reason': 'stop'}]}).encode())
        log = self.finish_log(observer)
        for value in ('completion_tokens=unavailable', 'reasoning_tokens=unavailable',
                      'reasoning_chars=0', 'finish_reason=stop', 'stream_status=complete'):
            self.assertIn(value, log)

    def test_incomplete_malformed_and_oversized_responses_remain_bounded(self):
        for wire in (b'data: {"choices": []}\n\n', b'data: PRIVATE broken JSON\n\n',
                     b'data: ' + b'x' * (MAX_EVENT_BYTES + 1)):
            with self.subTest(size=len(wire)):
                observer = self.observer()
                observer.start(200, 'text/event-stream')
                observer.feed(wire)
                self.assertLessEqual(len(observer.buffer) + observer.data_size, MAX_EVENT_BYTES)
                log = self.finish_log(observer)
                self.assertIn('stream_status=incomplete', log)
                self.assertNotIn('PRIVATE', log)

    def test_native_reasoning_tokens_are_used_when_reported(self):
        observer = self.observer()
        observer.start(200, 'application/json')
        observer.feed(json.dumps({'choices': [{'message': {'reasoning_content': PRIVATE},
                                               'finish_reason': 'stop'}],
                                 'usage': {'completion_tokens': 100,
                                           'completion_tokens_details': {
                                               'reasoning_tokens': 20}}}).encode())
        log = self.finish_log(observer)
        self.assertIn('reasoning_tokens=20', log)
        self.assertIn(f'reasoning_chars={len(PRIVATE)}', log)

    def test_interrupted_upstream_and_disconnected_client_are_distinct(self):
        observer = self.observer()
        observer.start(200, 'text/event-stream')
        observer.feed(stream_bytes())
        with self.assertLogs(LOGGER, 'INFO') as captured:
            observer.finish(upstream_complete=True, client_connected=False)
        self.assertIn('stream_status=complete client_disconnected=true', captured.output[0])
        with self.assertLogs(LOGGER, 'INFO') as captured:
            self.observer().finish(upstream_complete=False, client_connected=True)
        self.assertIn('stream_status=interrupted client_disconnected=false', captured.output[0])

    def test_untrusted_metadata_and_http_errors_never_enter_logs(self):
        observer = self.observer(max_tokens=PRIVATE, reasoning_effort=PRIVATE,
                                 chat_template_kwargs={'enable_thinking': PRIVATE})
        observer.start(200, 'application/json')
        observer.feed(json.dumps({'choices': [{'message': {'reasoning_content': PRIVATE},
                                               'finish_reason': PRIVATE}],
                                  'usage': {'completion_tokens': PRIVATE}}).encode())
        log = self.finish_log(observer)
        self.assertIn('finish_reason=other', log)
        self.assertIn('completion_tokens=unavailable', log)
        observer = self.observer()
        observer.start(500, 'application/json')
        observer.feed(json.dumps({'error': PRIVATE}).encode())
        self.assertIn('stream_status=http_error', self.finish_log(observer))

    def test_usage_request_preserves_other_options_and_invalid_values(self):
        payload = {'stream': True, 'stream_options': {'include_usage': False, 'custom': 7},
                   'messages': [PRIVATE]}
        original = json.dumps(payload)
        self.assertEqual(json.loads(request_usage(payload))['stream_options'],
                         {'include_usage': True, 'custom': 7})
        self.assertEqual(json.dumps(payload), original)
        for options in ('invalid', False, []):
            payload = {'stream': True, 'stream_options': options}
            self.assertEqual(json.loads(request_usage(payload)), payload)
