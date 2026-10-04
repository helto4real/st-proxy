from __future__ import annotations

import json
import runpy
from pathlib import Path

from st_proxy.kobold_router import default_model_body


def test_bootstrap_keeps_access_controls_but_no_models_or_startup_actions(tmp_path):
    module = runpy.run_path(str(Path(__file__).parents[1] / 'scripts/kobold_bootstrap.py'))
    profile = {
        'model': '/synthetic/text.gguf', 'model_param': '/synthetic/text.gguf',
        'sdmodel': '/synthetic/image.gguf', 'mmproj': '/synthetic/mmproj.gguf',
        'draftmodel': '/synthetic/draft.gguf', 'ttsmodel': '/synthetic/tts.gguf',
        'embeddingsmodel': '/synthetic/embed.gguf', 'whispermodel': '/synthetic/stt.gguf',
        'musicllm': '/synthetic/music.gguf', 'musicdiffusion': '/synthetic/diff.gguf',
        'mcpfile': '/synthetic/tools.json', 'onready': 'synthetic-command',
        'usecuda': ['mmq'], 'autoswapmode': True, 'launch': True,
        'host': '127.0.0.1', 'port': 61234, 'password': 'synthetic-api-secret',
        'adminpassword': 'synthetic-admin-secret', 'ssl': ['cert', 'key'],
        'contextsize': 32768,
    }
    source, output = tmp_path / 'profile.kcpps', tmp_path / 'bootstrap.json'
    source.write_text(json.dumps(profile))
    original = source.read_bytes()
    module['write_bootstrap'](source, output)
    assert source.read_bytes() == original
    assert json.loads(output.read_text()) == {
        'host': '127.0.0.1', 'port': 61234, 'password': 'synthetic-api-secret',
        'adminpassword': 'synthetic-admin-secret', 'ssl': ['cert', 'key'],
        'nomodel': True, 'usecpu': True, 'admin': True, 'routermode': True,
    }


def test_initial_profile_alias_does_not_change_explicit_models_or_sampling():
    for model in (None, '', 'initial_model'):
        payload = {'model': model, 'temperature': 0.7, 'max_tokens': 200}
        expected = {**payload, 'model': 'selected.kcpps'}
        actual = default_model_body(json.dumps(payload).encode(), 'selected.kcpps')
        assert json.loads(actual) == expected
    raw = b'{"model": "other.kcpps", "temperature": 0.7}'
    assert default_model_body(raw, 'selected.kcpps') == raw
