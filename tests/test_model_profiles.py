from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import ClientSession

from st_proxy.config import BrokerConfig, TestEndpointRegistry
from st_proxy.errors import ConfigurationError
from st_proxy.model_profiles import ModelProfiles
from st_proxy.service import BrokerService

from .strata_support import MockStrata
from .support import DynamicServer, MockComfy
from .tabby_support import MockTabby
from .test_prism_backend import MockPrism

SHIPPED = Path(__file__).resolve().parents[1] / 'config/model-profiles.json'


def write_profiles(tmp_path, *, engine='sc117', model='synthetic', defaults=None, **extra):
    profile = {'id': 'synthetic', 'models': [f'{engine}/{model}'],
               'defaults': defaults if defaults is not None else {
                   'temperature': 1, 'top_p': 0.95, 'top_k': 20, 'min_p': 0,
                   'repetition_penalty': 1, 'presence_penalty': 0, 'frequency_penalty': 0},
               'thinking': {'temperature': 1}, 'non_thinking': {'temperature': 0.7}, **extra}
    path = tmp_path / 'profiles.json'
    path.write_text(json.dumps({'version': 1, 'profiles': [profile]}))
    return path


def test_missing_values_and_every_explicit_value_win(tmp_path):
    profiles = ModelProfiles(str(write_profiles(tmp_path)))
    for value in (0, 0.4, None, False, 'invalid'):
        payload = {key: value for key in ('temperature', 'top_p', 'top_k', 'min_p',
                                         'repetition_penalty', 'presence_penalty',
                                         'frequency_penalty')}
        payload.update(messages=[{'role': 'user', 'content': 'private'}],
                       tools=[{'type': 'function', 'function': {'name': 'test'}}],
                       max_tokens=2000, stream=True, unknown={'keep': True})
        before = copy.deepcopy(payload)
        assert profiles.apply(payload, 'sc117', 'synthetic') == before
        assert payload == before
    assert profiles.apply({}, 'sc117', 'synthetic')['temperature'] == 1


@pytest.mark.parametrize('engine,wire', [('prism', 'repeat_penalty'),
                                       ('sc117', 'repetition_penalty'),
                                       ('tabbyapi', 'repetition_penalty')])
@pytest.mark.parametrize('alias', ['repeat_penalty', 'repetition_penalty', 'rep_pen'])
@pytest.mark.parametrize('value', [1.15, 0, None, 'invalid'])
def test_repetition_aliases_use_caller_value(tmp_path, engine, wire, alias, value):
    profiles = ModelProfiles(str(write_profiles(tmp_path, engine=engine)))
    original = {alias: value}
    result = profiles.apply(original, engine, 'synthetic')
    assert result[wire] == value
    assert result[alias] == value
    assert original == {alias: value}


def test_native_alias_priority_and_tabby_rep_pen(tmp_path):
    profiles = ModelProfiles(str(write_profiles(tmp_path, engine='tabbyapi')))
    result = profiles.apply({'rep_pen': 1.2, 'repeat_penalty': 1.4}, 'tabbyapi', 'synthetic')
    assert result['repetition_penalty'] == 1.2
    result = profiles.apply({'repetition_penalty': None, 'rep_pen': 1.2}, 'tabbyapi', 'synthetic')
    assert result['repetition_penalty'] is None


@pytest.mark.parametrize('controls,expected', [
    ({'reasoning_effort': 'low'}, 1),
    ({'reasoning_effort': 'none'}, 0.7),
    ({'reasoning': {'effort': 'medium'}}, 1),
    ({'chat_template_kwargs': {'enable_thinking': False}}, 0.7),
    ({'chat_template_kwargs': {'reasoning_effort': 'high'}}, 1),
    ({'reasoning_effort': 'low', 'chat_template_kwargs': {'enable_thinking': False}}, None),
    ({}, None), ({'reasoning_effort': None}, None),
    ({'reasoning_effort': 'unexpected'}, None),
    ({'enable_thinking': False}, None),  # Not consumed by Strata's Chat API.
    ({'reasoning': {'enabled': False}}, None),
    ({'chat_template_kwargs': {'enable_thinking': True}}, None),
    ({'reasoning_budget_tokens': 500}, None),
])
def test_mode_specific_defaults_without_changing_thinking(tmp_path, controls, expected):
    profiles = ModelProfiles(str(write_profiles(tmp_path, defaults={'top_k': 20})))
    result = profiles.apply(controls, 'sc117', 'synthetic')
    assert result.get('temperature') == expected
    assert result['top_k'] == 20
    for key, value in controls.items():
        assert result[key] == value
    assert 'reasoning_budget_tokens' not in result or 'reasoning_budget_tokens' in controls


@pytest.mark.parametrize('engine,controls,expected', [
    ('prism', {'reasoning_effort': 'none'}, 0.7),
    ('prism', {'reasoning_effort': 'medium'}, 1),
    ('prism', {'reasoning_effort': 'off'}, None),
    ('prism', {'reasoning_effort': 'high'}, None),
    ('prism', {'reasoning_effort': 'MEDIUM'}, None),
    ('prism', {'reasoning': {'effort': 'medium'}}, None),
    ('prism', {'chat_template_kwargs': {'reasoning_effort': 'none'}}, None),
    ('prism', {'chat_template_kwargs': {'enable_thinking': False}}, 0.7),
    ('prism', {'chat_template_kwargs': {'enable_thinking': True}}, 1),
    ('sc117', {'chat_template_kwargs': {'enabled': False}}, None),
    ('sc117', {'chat_template_kwargs': {'effort': 'medium'}}, None),
    ('tabbyapi', {'enable_thinking': False}, 0.7),
    ('tabbyapi', {'reasoning': {'enabled': True}}, 1),
    ('tabbyapi', {'template_vars': {'enable_thinking': False}}, 0.7),
    ('tabbyapi', {'template_vars': {},
                 'chat_template_kwargs': {'enable_thinking': False}}, None),
    ('tabbyapi', {'enable_thinking': True, 'reasoning_effort': 'off'}, None),
])
def test_mode_recognition_respects_backend_controls(tmp_path, engine, controls, expected):
    profiles = ModelProfiles(str(write_profiles(tmp_path, engine=engine, defaults={})))
    result = profiles.apply(controls, engine, 'synthetic')
    assert result.get('temperature') == expected
    assert {key: result[key] for key in controls} == controls


@pytest.mark.parametrize('controls', [
    {'reasoning_budget_tokens': 0}, {'reasoning_budget': None}, {'thinking_budget': -1},
    {'thinking_token_budget': 200}, {'thinking_budget_tokens': 400},
    {'reasoning': {'max_tokens': 700}}, {'thinking': {'budget_tokens': 800}},
    {'reasoning_effort': 'low'}, {'enable_thinking': False},
    {'chat_template_kwargs': {'enable_thinking': False}}, {'reasoning': None},
])
def test_optional_budget_never_overrides_caller_thinking(tmp_path, controls):
    path = write_profiles(tmp_path, defaults={'reasoning_budget_tokens': 500})
    profiles = ModelProfiles(str(path))
    assert profiles.apply(controls, 'sc117', 'synthetic') == {
        **controls, **({'temperature': 1} if controls.get('reasoning_effort') == 'low' else
                      {'temperature': 0.7} if controls.get('chat_template_kwargs') else {})}
    assert profiles.apply({}, 'sc117', 'synthetic') == {'reasoning_budget_tokens': 500}


def test_shipped_profiles_have_no_budget_or_output_limit():
    data = json.loads(SHIPPED.read_text())
    profiles = ModelProfiles(str(SHIPPED))
    models = [model for p in data['profiles'] for model in p['models']]
    assert len(models) == len(set(models)) == 8
    for p in data['profiles']:
        assert p['sources'] and p['checked_at']
        for qualified in p['models']:
            engine, model = qualified.split('/', 1)
            for controls in ({}, {'reasoning_effort': 'medium'}, {'reasoning_effort': 'none'}):
                result = profiles.apply(controls, engine, model)
                assert not set(result) & {'reasoning_budget_tokens', 'max_tokens',
                                          'max_completion_tokens', 'enable_thinking'}
                assert result['top_k'] == 20
                if controls.get('reasoning_effort') == 'medium':
                    assert result['min_p'] == (0.05 if engine == 'prism' else 0)
                elif controls.get('reasoning_effort') == 'none':
                    assert result['temperature'] == 0.7
                    assert result['presence_penalty'] == 1.5
                else:
                    assert 'temperature' not in result
    assert profiles.apply({}, 'prism', 'prism-bonsai-2-27b-other') == {}


def test_unmatched_disabled_and_unparseable_requests_are_byte_preserved(tmp_path):
    profiles = ModelProfiles(str(write_profiles(tmp_path)))
    for raw in (b'{ "model": "another", "unknown": 42 }', b'{bad', b'[]', b'null'):
        assert profiles.apply_body(raw, 'sc117') == raw
    raw = b'{ "model": "synthetic", "temperature": 0 }'
    assert profiles.apply_body(raw, 'prism') == raw
    assert ModelProfiles().apply_body(raw, 'sc117') == raw
    assert ModelProfiles('').apply_body(raw, 'sc117') == raw


@pytest.mark.parametrize('parameter,value,engine', [
    ('temperature', True, 'prism'), ('temperature', float('nan'), 'prism'),
    ('top_p', 0, 'prism'), ('min_p', 1.1, 'prism'),
    ('top_k', 1.5, 'prism'), ('top_k', 65, 'sc117'),
    ('repetition_penalty', 0, 'prism'), ('reasoning_budget_tokens', -1, 'sc117'),
    ('frequency_penalty', -1, 'sc117'), ('presence_penalty', 3, 'prism'),
    ('unknown', 1, 'prism'), ('max_tokens', 2000, 'prism'),
])
def test_invalid_profile_parameters_fail(tmp_path, parameter, value, engine):
    path = write_profiles(tmp_path, engine=engine, defaults={parameter: value})
    with pytest.raises(ConfigurationError):
        ModelProfiles(str(path))


def test_duplicate_models_keys_unsupported_backend_and_bad_file_fail(tmp_path):
    path = write_profiles(tmp_path)
    data = json.loads(path.read_text())
    data['profiles'].append({**data['profiles'][0], 'id': 'other'})
    for invalid in (json.dumps(data), '{"version":1,"version":1,"profiles":[]}', '{}',
                    '{"version":true,"profiles":[]}'):
        path.write_text(invalid)
        with pytest.raises(ConfigurationError):
            ModelProfiles(str(path))
    for engine in ('unknown', 'ollama'):
        path = write_profiles(tmp_path, engine=engine)
        with pytest.raises(ConfigurationError):
            ModelProfiles(str(path))
    with pytest.raises(ConfigurationError):
        ModelProfiles(str(tmp_path / 'missing.json'))


def test_debug_log_contains_only_profile_and_field_names(tmp_path, caplog):
    profiles = ModelProfiles(str(write_profiles(tmp_path)))
    with caplog.at_level('DEBUG', logger='st_proxy.model_profiles'):
        profiles.apply({'messages': [{'content': 'PRIVATE'}]}, 'sc117', 'synthetic')
    assert 'default_fields=' in caplog.text
    assert 'PRIVATE' not in caplog.text


@pytest.mark.parametrize('engine', [
    'prism', 'sc117', 'orca', 'sc117_128k', 'orca_128k', 'tabbyapi',
])
def test_profiles_reach_separate_backend_and_preserve_response(tmp_path, engine):
    async def scenario():
        mocks = {'prism': MockPrism, 'tabbyapi': MockTabby}
        mock = mocks.get(engine, MockStrata)()
        profile = write_profiles(tmp_path, engine=engine, model=mock.model)
        registry = TestEndpointRegistry()
        server, comfy = DynamicServer(mock.app()), DynamicServer(MockComfy().app())
        service = None
        try:
            origin = registry.approve_disposable(await server.start())
            comfy_origin = registry.approve_disposable(await comfy.start())
            config = BrokerConfig.for_test(llm_backend=engine, llm_url=origin,
                                           comfy_url=comfy_origin, registry=registry,
                                           prism_model=mock.model, tabby_model=mock.model,
                                           model_profiles=str(profile))
            service = BrokerService(config)
            await service.start()
            url = f'http://127.0.0.1:{service.chat_port}'
            async with ClientSession() as client:
                request = {'model': mock.model,
                           'messages': [{'role': 'user', 'content': 'synthetic'}],
                           'temperature': 0, 'max_tokens': 2000, 'reasoning_effort': 'none',
                           'tools': [], 'unknown': {'keep': True}}
                async with client.post(url + '/v1/chat/completions', json=request) as response:
                    assert response.status == 200
                    assert 'synthetic' in await response.text()
                sent = (json.loads(mock.chat_payloads[-1]) if engine == 'prism' else
                        json.loads(mock.payloads[-1]) if engine == 'tabbyapi'
                        else mock.payloads[-1])
                for key, value in request.items():
                    assert sent[key] == value
                assert sent['top_p'] == 0.95
                assert sent['top_k'] == 20
                assert 'reasoning_budget_tokens' not in sent
                wire = 'repeat_penalty' if engine == 'prism' else 'repetition_penalty'
                assert sent[wire] == 1
                if engine != 'tabbyapi':
                    omitted_model = {key: value for key, value in request.items() if key != 'model'}
                    async with client.post(url + '/v1/chat/completions',
                                           json=omitted_model) as response:
                        assert response.status == 200
                        await response.read()
                    resolved = (json.loads(mock.chat_payloads[-1]) if engine == 'prism'
                                else mock.payloads[-1])
                    assert resolved['model'] == mock.model
                    assert resolved['top_k'] == 20
                if engine in ('sc117', 'orca', 'sc117_128k', 'orca_128k'):
                    async with client.post(url + '/v1/messages', json=request) as response:
                        assert response.status == 200
                        await response.read()
                    assert mock.payloads[-1] == request
        finally:
            if service:
                await service.stop()
            await server.close()
            await comfy.close()
    asyncio.run(scenario())


def test_bad_profile_fails_before_network_start(tmp_path):
    async def scenario():
        service = BrokerService(replace(BrokerConfig(), model_profiles=str(tmp_path / 'missing')))
        with patch.object(service, '_new_session', side_effect=AssertionError('No network')):
            with pytest.raises(ConfigurationError):
                await service.start()
        assert service.coordinator is None
    asyncio.run(scenario())
