from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web

from st_proxy.workflows import Workflow, WorkflowCatalog, WorkflowError

from . import test_media_jobs as jobs_fixture
from . import test_reference_images as refs_fixture

ROOT = Path(__file__).resolve().parents[1] / 'workflows'


def write_upscale(directory):
    graph, _ = refs_fixture.write_references(directory)
    graph['2'] = graph.pop('3')
    graph['6'] = graph.pop('9')
    graph['1']['inputs'].pop('image 1')
    graph['1']['inputs'].update({
        'positive_prompt': '{"encrypted":true,"ciphertext":"SYNTHETIC_STORED_PROMPT"}',
        'sampler': 'euler', 'size mode': 'use image 1 aspect ratio',
        'max side': 3200,
    })
    graph['4']['inputs'].update(color_match=True, color_match_strength=1,
                                contrast_strength=0.5, microcontrast_strength=0)
    manifest = json.loads((ROOT / 'image_upscale.manifest.json').read_text())
    manifest['workflow'] = 'workflow.json'
    (directory / 'workflow.json').write_text(json.dumps(graph))
    (directory / 'example.manifest.json').write_text(json.dumps(manifest))
    return graph, manifest


def test_shipped_upscale_contract_preserves_the_export():
    original = json.loads((ROOT / 'image_upscale.json').read_text())
    workflow = WorkflowCatalog(str(ROOT), 100000).get('image_upscale')
    assert workflow.normalize({}) == {'max_side': 3200}
    description = workflow.describe()
    assert set(description['parameters']['properties']) == {'max_side', 'seed'}
    assert description['parameters']['properties']['max_side']['maximum'] == 4096
    assert description['references']['minItems'] == description['references']['maxItems'] == 1
    assert 'without references' not in description['references']['description']
    assert 'prompt_guidance' not in description
    reference = {'root': '/synthetic/input', 'subfolder': 'references', 'filename': 'input.png'}
    expected = copy.deepcopy(original)
    expected['1']['inputs']['max side'] = 4096
    expected['1']['inputs']['image 1'] = ['mcp_reference_1', 0]
    expected['mcp_reference_1'] = copy.deepcopy(original['6'])
    expected['mcp_reference_1']['inputs'].update(reference)
    expected['2']['inputs']['reference_only'] = True
    assert workflow.build(workflow.normalize({
        'max_side': 4096, 'seed': original['1']['inputs']['seed'],
    }), [reference]) == expected
    assert workflow.graph == original
    assert WorkflowCatalog(str(ROOT), 100000).get('qwen_scene').describe()['references'][
        'minItems'] == 0


@pytest.mark.parametrize('minimum', [-1, 2, True, 0.5, '1', None])
def test_invalid_reference_minimum_is_rejected(tmp_path, minimum):
    _, manifest = write_upscale(tmp_path)
    manifest['references']['minItems'] = minimum
    with pytest.raises(WorkflowError, match='minimum reference'):
        Workflow.load(manifest, tmp_path, 100000)


@pytest.mark.parametrize('parameters', [
    {'max_side': 255}, {'max_side': 4128}, {'max_side': 1000}, {'max_side': True},
    {'prompt': 'replace'}, {'mode': 'standard'}, {'aspect_ratio': '16:9'},
])
def test_upscale_only_accepts_bounded_max_side(tmp_path, parameters):
    _, manifest = write_upscale(tmp_path)
    workflow = Workflow.load(manifest, tmp_path, 100000)
    with pytest.raises(WorkflowError):
        workflow.normalize(parameters)


def test_build_requires_input_and_optional_workflows_still_accept_none(tmp_path):
    _, manifest = write_upscale(tmp_path)
    workflow = Workflow.load(manifest, tmp_path, 100000)
    with pytest.raises(WorkflowError, match='at least 1'):
        workflow.build(workflow.normalize({'seed': 0}))
    del manifest['references']['minItems']
    optional = Workflow.load(manifest, tmp_path, 100000)
    assert 'image 1' not in optional.build(optional.normalize({'seed': 0}))['1']['inputs']


class UpscaleMCPTests(unittest.IsolatedAsyncioTestCase):
    sdk_client = refs_fixture.ReferenceJobsTests.sdk_client
    url = refs_fixture.ReferenceJobsTests.url

    async def asyncSetUp(self):
        original_history = jobs_fixture.ImageComfy.history

        async def history(comfy, request):
            response = await original_history(comfy, request)
            payload = json.loads(response.body)
            for entry in payload.values():
                outputs = entry.get('outputs', {})
                if '3' in outputs:
                    outputs['2'] = outputs.pop('3')
            return web.json_response(payload, status=response.status)

        patcher = patch.object(jobs_fixture.ImageComfy, 'history', history)
        patcher.start()
        self.addCleanup(patcher.stop)
        await refs_fixture.ReferenceJobsTests.asyncSetUp(self)
        self.graph, _ = write_upscale(self.catalog_dir)
        self.jobs.catalog = WorkflowCatalog(str(self.catalog_dir), 100000)

    async def test_sdk_registration_upscale_batch_and_recovery(self):
        original_bytes = self.source.read_bytes()
        async with self.sdk_client() as client:
            listing = await client.call_tool('list_workflows', {})
            self.assertEqual(listing.structured_content['workflows'][0]['id'], 'image_upscale')
            description = await client.call_tool('describe_workflow', {
                'workflow_id': 'image_upscale',
            })
            self.assertEqual(description.structured_content['references']['minItems'], 1)
            registered = await client.call_tool('register_image', {
                'workflow_id': 'image_upscale', 'path': str(self.source),
            })
            self.assertFalse(registered.is_error, registered)
            reference = registered.structured_content
            self.assertFalse(self.llm.admin_calls)
            request = {'job_id': 'upscale', 'workflow_id': 'image_upscale',
                       'parameters': {}, 'references': [reference['image_id']]}
            response = await client.call_tool('generate', request)
            self.assertFalse(response.is_error, response)
            result = response.structured_content
            self.assertEqual(result['status'], 'succeeded')
            self.assertEqual(result['parameters']['max_side'], 3200)
            self.assertIsInstance(result['parameters']['seed'], int)
            self.assertNotIn('SYNTHETIC_STORED_PROMPT', json.dumps(result))
            self.assertEqual(Path(result['images'][0]['path']).read_bytes(), jobs_fixture.PNG)
            sent = self.comfy.submissions[0]['prompt']
            expected = copy.deepcopy(self.graph)
            expected['1']['inputs']['seed'] = result['parameters']['seed']
            expected['2']['inputs']['reference_only'] = True
            node = sent['1']['inputs']['image 1'][0]
            expected['1']['inputs']['image 1'] = [node, 0]
            expected[node] = copy.deepcopy(expected['6'])
            expected[node]['inputs'].update(root=str(self.jobs.output_dir),
                                            subfolder='references',
                                            filename=Path(reference['preview_path']).name)
            self.assertEqual(sent, expected)
            recovered = await client.call_tool('get_job', {'job_id': 'upscale'})
            repeated = await client.call_tool('generate', request)
            self.assertEqual(recovered.structured_content, result)
            self.assertEqual(repeated.structured_content, result)
            self.assertEqual(len(self.comfy.submissions), 1)
            maximum = await client.call_tool('generate', {
                **request, 'job_id': 'upscale-maximum', 'parameters': {'max_side': 4096},
            })
            self.assertFalse(maximum.is_error, maximum)
            self.assertEqual(maximum.structured_content['parameters']['max_side'], 4096)
            expected['1']['inputs']['seed'] = maximum.structured_content['parameters']['seed']
            expected['1']['inputs']['max side'] = 4096
            self.assertEqual(self.comfy.submissions[1]['prompt'], expected)
            self.comfy.fail_next_job = True
            batch_request = {'job_id': 'upscale-batch', 'workflow_id': 'image_upscale',
                             'items': [{'parameters': {'max_side': size},
                                        'references': [reference['image_id']]}
                                       for size in (2560, 4096)]}
            with patch.object(self.jobs.coordinator, 'image_batch',
                              wraps=self.jobs.coordinator.image_batch) as reservation:
                batch = await client.call_tool('generate_batch', batch_request)
                reservation.assert_called_once()
            self.assertTrue(batch.is_error, batch)
            self.assertEqual(batch.structured_content['status'], 'partial')
            self.assertEqual([i['status'] for i in batch.structured_content['items']],
                             ['failed', 'succeeded'])
            for submission, size, item in zip(
                self.comfy.submissions[2:], (2560, 4096), batch.structured_content['items'],
                strict=True,
            ):
                sent = submission['prompt']
                self.assertEqual(sent['1']['inputs']['max side'], size)
                self.assertEqual(sent['1']['inputs']['seed'], item['parameters']['seed'])
                self.assertEqual(sent['1']['inputs']['positive_prompt'],
                                 self.graph['1']['inputs']['positive_prompt'])
                self.assertEqual(sent[sent['1']['inputs']['image 1'][0]]['inputs']['filename'],
                                 Path(reference['preview_path']).name)
            self.assertEqual((await client.call_tool('get_job', {
                'job_id': 'upscale-batch',
            })).structured_content, batch.structured_content)
            self.assertEqual((await client.call_tool('generate_batch', batch_request)
                              ).structured_content, batch.structured_content)
            self.assertEqual(len(self.comfy.submissions), 4)
            self.assertEqual(self.llm.admin_calls, ['unload_model'])
            self.assertEqual(self.comfy.free_calls, 0)
            self.assertEqual(self.source.read_bytes(), original_bytes)

    async def test_reference_errors_reject_entire_batch_before_queueing(self):
        ref = await self.jobs.register_image('image_upscale', str(self.source))
        async with self.sdk_client() as client:
            for refs in ([], [ref['image_id']] * 2, ['unknown']):
                with self.subTest(refs=refs):
                    single = await client.call_tool('generate', {
                        'job_id': 'bad', 'workflow_id': 'image_upscale',
                        'parameters': {}, 'references': refs,
                    })
                    batch = await client.call_tool('generate_batch', {
                        'job_id': 'bad-batch', 'workflow_id': 'image_upscale', 'items': [
                            {'parameters': {}, 'references': [ref['image_id']]},
                            {'parameters': {}, 'references': refs},
                        ],
                    })
                    self.assertTrue(single.is_error)
                    self.assertTrue(batch.is_error)
            missing = await client.call_tool('generate', {
                'job_id': 'missing', 'workflow_id': 'image_upscale', 'parameters': {},
            })
            self.assertTrue(missing.is_error)
            self.assertIn('at least 1', missing.structured_content['error']['message'])
        self.assertFalse(self.jobs.jobs)
        self.assertFalse(self.comfy.prompt_calls)
        self.assertFalse(self.llm.admin_calls)
