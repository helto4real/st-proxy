from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web
from PIL import Image

from st_proxy import reference_images
from st_proxy.media_jobs import JobError
from st_proxy.reference_images import ReferenceImages, _normalize
from st_proxy.workflows import Workflow, WorkflowCatalog, WorkflowError

from . import test_mcp_server as transport
from . import test_media_jobs as fixtures
from .test_workflows import write_catalog


def write_references(directory):
    graph, manifest = write_catalog(directory)
    graph['9'] = {'class_type': 'HeltoLoadImageFromRoot', 'inputs': {
        'root': '/unused/template/root', 'subfolder': 'unused', 'filename': '',
        'future_option': {'opaque': True},
    }}
    graph['mcp_reference_1'] = {'opaque': ['keep', 'collision']}
    graph['unmapped'] = {'future': ['unvalidated', {'data': True}]}
    graph['1']['inputs']['image 1'] = ['old-reference', 0]
    graph['1']['inputs']['unmapped_image'] = ['keep', 7]
    manifest['references'] = {'adapter': 'helto_root_images', 'generator': '1', 'loader': '9',
                              'inputs': [f'image {i}' for i in range(1, 11)]}
    manifest['prompt_guidance'] = 'guidance.md'
    (directory / 'guidance.md').write_bytes(b'\nSynthetic guidance\r\nKeep exact text.\n')
    (directory / 'workflow.json').write_text(json.dumps(graph))
    (directory / 'example.manifest.json').write_text(json.dumps(manifest))
    return graph, manifest


def image_bytes(color='red', format='PNG', **kwargs):
    stream = io.BytesIO()
    Image.new('RGB', (16, 8), color).save(stream, format=format, **kwargs)
    return stream.getvalue()


@pytest.mark.parametrize('count', [0, 1, 10])
def test_reference_mapping_is_ordered_and_only_changes_owned_inputs(tmp_path, count):
    graph, manifest = write_references(tmp_path)
    workflow = Workflow.load(manifest, tmp_path, 100000)
    refs = [{'root': '/synthetic/results', 'subfolder': 'references', 'filename': f'{i}.png'}
            for i in range(count)]
    result = workflow.build(workflow.normalize({'prompt': 'scene'}), refs)
    expected = copy.deepcopy(graph)
    expected['1']['inputs'].pop('image 1')
    expected['1']['inputs'].update(positive_prompt='scene', seed=42)
    expected['3']['inputs']['reference_only'] = True
    for i, ref in enumerate(refs):
        node = f'mcp_reference_{i + 1}' + ('_' if i == 0 else '')
        expected[node] = copy.deepcopy(graph['9'])
        expected[node]['inputs'].update(ref)
        expected['1']['inputs'][f'image {i + 1}'] = [node, 0]
    assert result == expected
    assert workflow.graph == graph
    assert workflow.describe()['prompt_guidance'] == '\nSynthetic guidance\r\nKeep exact text.\n'
    assert 'prompt_guidance' not in workflow.summary()


def test_reference_contract_validation_stays_local(tmp_path):
    graph, manifest = write_references(tmp_path)
    for change in ({'inputs': ['image 1'] * 2}, {'generator': 'missing'}, {'loader': 'missing'},
                   {'inputs': ['x'] * 11}, {'inputs': []}, {'inputs': [None]}):
        altered = copy.deepcopy(manifest)
        altered['references'].update(change)
        with pytest.raises(WorkflowError):
            Workflow.load(altered, tmp_path, 100000)
    graph['9']['class_type'] = 'WrongLoader'
    (tmp_path / 'workflow.json').write_text(json.dumps(graph))
    with pytest.raises(WorkflowError, match='template'):
        Workflow.load(manifest, tmp_path, 100000)


def test_prompt_guidance_is_bounded_and_cannot_escape_catalog(tmp_path):
    _, manifest = write_references(tmp_path)
    manifest['prompt_guidance'] = '../outside.md'
    with pytest.raises(WorkflowError, match='inside'):
        Workflow.load(manifest, tmp_path, 100000)
    manifest['prompt_guidance'] = 'guidance.md'
    (tmp_path / 'guidance.md').write_bytes(b'x' * 65537)
    with pytest.raises(WorkflowError, match='64 KiB'):
        Workflow.load(manifest, tmp_path, 100000)


def test_supplied_scene_contract_and_prompt_are_loadable():
    root = Path(__file__).resolve().parents[1] / 'workflows'
    catalog = WorkflowCatalog(str(root), 64 * 1024**2)
    scene, original = catalog.get('qwen_scene'), catalog.get('image_generation')
    prompt = (root / 'prompts/reference-aware-scene.md').read_bytes().decode()
    assert scene.prompt_guidance == prompt
    assert scene.parameters['prompt']['maxLength'] == 12000
    assert scene.parameters['max_side']['maximum'] == 3200
    assert scene.graph['4']['inputs']['edit_canvas'] == 'main controls'
    assert original.references is None
    assert original.parameters['prompt']['maxLength'] == 12000
    text_guidance = (root / 'prompts/prompt-enhance-scene.md').read_bytes().decode()
    assert original.describe()['prompt_guidance'] == text_guidance
    for name in ('aspect_ratio', 'max_side'):
        assert scene.parameters[name] == original.parameters[name]


class ReferenceJobsTests(unittest.IsolatedAsyncioTestCase):
    sdk_client = transport.MCPTransportTests.sdk_client
    url = transport.MCPTransportTests.url

    async def asyncSetUp(self):
        self.store_calls = []
        self.store_status = 200
        self.store_override = {}
        original = fixtures.ImageComfy.generic

        async def generic(comfy, request):
            if request.path != '/helto_load_image_from_root/store':
                return await original(comfy, request)
            form = await request.post()
            payload = form['image'].file.read()
            self.store_calls.append((dict(form), payload))
            if self.store_status != 200:
                return web.json_response({'error': 'synthetic'}, status=self.store_status)
            with Image.open(io.BytesIO(payload)) as source:
                assert source.format == 'PNG'
                width, height = source.size
            digest = hashlib.sha256(payload).hexdigest()
            directory = Path(form['root']) / form['subfolder']
            directory.mkdir(exist_ok=True)
            (directory / f'{digest}.png').write_bytes(payload)
            return web.json_response({'filename': f'{digest}.png', 'imageId': f'sha256:{digest}',
                                      'mimeType': 'image/png', 'width': width, 'height': height,
                                      'byteSize': len(payload), **self.store_override})

        patcher = patch.object(fixtures.ImageComfy, 'generic', generic)
        patcher.start()
        self.addCleanup(patcher.stop)
        await fixtures.MediaJobsTests.asyncSetUp(self)
        write_references(self.catalog_dir)
        self.jobs.catalog = WorkflowCatalog(str(self.catalog_dir), 100000)
        self.inputs = self.directory / 'inputs'
        self.inputs.mkdir()
        self.jobs.references = ReferenceImages(
            replace(self.config, mcp_input_dir=str(self.inputs)), self.jobs.session,
        )
        self.source = self.inputs / 'image.jpg'
        self.source.write_bytes(image_bytes(format='JPEG'))

    async def register(self, path=None):
        return await self.jobs.register_image('example', str(path or self.source))

    async def test_registration_normalizes_without_gpu_and_sdk_generates(self):
        async with self.sdk_client() as client:
            registered = await client.call_tool('register_image', {
                'workflow_id': 'example', 'path': str(self.source),
            })
            self.assertFalse(registered.is_error, registered)
            ref = registered.structured_content
            self.assertEqual((ref['width'], ref['height']), (16, 8))
            self.assertEqual(ref['mime_type'], 'image/png')
            self.assertTrue(Path(ref['preview_path']).is_file())
            self.assertEqual(self.store_calls[0][0]['root'], str(self.directory / 'results'))
            self.assertEqual(self.store_calls[0][0]['subfolder'], 'references')
            self.assertFalse(self.llm.admin_calls)
            self.assertFalse(self.comfy.prompt_calls)
            self.assertFalse(self.comfy.free_calls)
            description = await client.call_tool('describe_workflow', {'workflow_id': 'example'})
            self.assertIn('Synthetic guidance', description.structured_content['prompt_guidance'])
            self.assertEqual(description.structured_content['references']['maxItems'], 10)
            self.assertNotIn('Synthetic guidance', str(await client.list_tools()))
            result = await client.call_tool('generate', {
                'job_id': 'scene', 'workflow_id': 'example', 'parameters': {'prompt': '<image1>'},
                'references': [ref['image_id']],
            })
            self.assertFalse(result.is_error, result)
            graph = self.comfy.submissions[0]['prompt']
            node = graph['1']['inputs']['image 1'][0]
            self.assertEqual(graph[node]['inputs']['filename'], Path(ref['preview_path']).name)
            self.assertEqual(self.llm.admin_calls, ['unload_model'])
            batch = await client.call_tool('generate_batch', {
                'job_id': 'sdk-reference-batch', 'workflow_id': 'example',
                'items': [{'parameters': {'prompt': '<image1>'}, 'references': [ref['image_id']]},
                          {'prompt': 'text only'}],
            })
            self.assertFalse(batch.is_error, batch)
            self.assertEqual(batch.structured_content['status'], 'succeeded')
            self.assertEqual(self.llm.admin_calls, ['unload_model'])

    async def test_batch_order_content_identity_partial_results_and_retry(self):
        first = await self.register()
        second_path = self.inputs / 'blue.webp'
        second_path.write_bytes(image_bytes('blue', 'WEBP'))
        second = await self.register(second_path)
        ids = [first['image_id'], second['image_id']]
        items = [{'parameters': {'prompt': '<image1> beside <image2>'}, 'references': ids},
                 {'parameters': {'prompt': '<image1>'}, 'references': [ids[1]]},
                 {'prompt': 'No references'}]
        self.comfy.fail_next_job = True
        result = await self.jobs.generate_batch('batch', 'example', items)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual([i['status'] for i in result['items']],
                         ['failed', 'succeeded', 'succeeded'])
        graph = self.comfy.submissions[0]['prompt']
        names = [graph[graph['1']['inputs'][f'image {i}'][0]]['inputs']['filename']
                 for i in (1, 2)]
        self.assertEqual(names, [Path(r['preview_path']).name for r in (first, second)])
        self.assertNotIn('image 1', self.comfy.submissions[2]['prompt']['1']['inputs'])
        same_content = await self.register()
        items[0]['references'] = [same_content['image_id'], ids[1]]
        self.assertEqual(await self.jobs.generate_batch('batch', 'example', items), result)
        items[0]['references'].reverse()
        with self.assertRaisesRegex(JobError, 'another request'):
            await self.jobs.generate_batch('batch', 'example', items)
        self.assertEqual(len(self.comfy.submissions), 3)
        self.assertEqual(self.llm.admin_calls, ['unload_model'])
        self.assertEqual(self.comfy.free_calls, 0)

    async def test_invalid_references_rejected_before_any_batch_admission(self):
        ref = await self.register()
        for refs in (['unknown'], [ref['image_id']] * 11, [17], 'not-a-list', None):
            with self.subTest(refs=refs), self.assertRaises(WorkflowError):
                await self.jobs.generate_batch('bad', 'example', [
                    {'prompt': 'valid'}, {'parameters': {'prompt': 'bad'}, 'references': refs},
                ])
        with self.assertRaisesRegex(JobError, 'unknown batch'):
            await self.jobs.generate_batch('bad', 'example', [
                {'parameters': {'prompt': 'bad'}, 'extra': 1},
            ])
        self.assertFalse(self.jobs.jobs)
        self.assertFalse(self.comfy.prompt_calls)
        self.assertFalse(self.llm.admin_calls)

    async def test_changed_missing_and_cross_workflow_references(self):
        ref = await self.register()
        other = replace(self.jobs.catalog.get('example'), manifest={
            **self.jobs.catalog.get('example').manifest, 'id': 'other',
        })
        self.jobs.catalog.workflows['other'] = other
        with self.assertRaisesRegex(WorkflowError, 'unknown'):
            await self.jobs.generate('bad', 'other', {'prompt': 'x'}, [ref['image_id']])
        path = Path(ref['preview_path'])
        path.write_bytes(image_bytes('blue'))
        with self.assertRaisesRegex(WorkflowError, 'changed'):
            await self.jobs.generate('bad', 'example', {'prompt': 'x'}, [ref['image_id']])
        path.unlink()
        with self.assertRaisesRegex(WorkflowError, 'unavailable'):
            await self.jobs.generate('bad', 'example', {'prompt': 'x'}, [ref['image_id']])
        self.assertFalse(self.comfy.prompt_calls)

    async def test_input_path_format_and_size_boundaries(self):
        outside = self.directory / 'outside.png'
        outside.write_bytes(image_bytes())
        link = self.inputs / 'escape.png'
        link.symlink_to(outside)
        fake = self.inputs / 'fake.png'
        fake.write_bytes(b'not a picture')
        gif = self.inputs / 'unsupported.png'
        gif.write_bytes(image_bytes(format='GIF'))
        empty = self.inputs / 'empty.png'
        empty.touch()
        oversized = self.inputs / 'large.png'
        oversized.write_bytes(b'x' * (reference_images.MAX_REFERENCE_BYTES + 1))
        for path in (outside, link, fake, gif, empty, oversized, self.inputs, Path('relative.png')):
            with self.subTest(path=path), self.assertRaises(WorkflowError):
                await self.register(path)
        self.assertFalse(self.store_calls)
        self.assertFalse(self.llm.admin_calls)
        # Generated images are also accepted, independent of their file extension.
        output = self.directory / 'results' / 'generated.bin'
        output.write_bytes(image_bytes())
        await self.register(output)

    async def test_store_errors_are_sanitized_and_do_not_register(self):
        self.store_status = 500
        with self.assertRaisesRegex(WorkflowError, 'rejected'):
            await self.register()
        self.store_status = 200
        for override in ({'filename': '../escape.png'}, {'width': True}, {'width': 99},
                         {'imageId': 'sha256:wrong'}, {'byteSize': 0}, {'mimeType': 'text/plain'}):
            self.store_override = override
            with self.subTest(override=override), self.assertRaisesRegex(WorkflowError, 'invalid'):
                await self.register()
        self.assertFalse(self.jobs.references.images)
        self.assertFalse(self.comfy.prompt_calls)
        self.assertFalse(self.llm.admin_calls)


def test_exif_orientation_and_normalized_limit(tmp_path):
    source = tmp_path / 'rotated.jpg'
    exif = Image.Exif()
    exif[274] = 6
    source.write_bytes(image_bytes(format='JPEG', exif=exif))
    png, width, height = _normalize(source, (tmp_path,))
    assert (width, height) == (8, 16)
    with Image.open(io.BytesIO(png)) as image:
        assert not image.getexif()
    # A compressed input can expand beyond the transport cap when converted to PNG.
    noisy = Image.effect_noise((128, 128), 100).convert('RGB')
    noisy.save(source, format='JPEG', quality=10)
    with patch.object(reference_images, 'MAX_REFERENCE_BYTES', source.stat().st_size):
        with pytest.raises(WorkflowError, match='normalized'):
            _normalize(source, (tmp_path,))


def test_parent_symlink_swap_cannot_escape_allowed_root(tmp_path):
    root = tmp_path / 'inputs'
    nested = root / 'nested'
    nested.mkdir(parents=True)
    (nested / 'image.png').write_bytes(image_bytes())
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'image.png').write_bytes(image_bytes('blue'))
    real_open = os.open

    def swap(path, flags, *args, **kwargs):
        if path == root:
            nested.rename(root / 'old')
            nested.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    with patch.object(reference_images.os, 'open', side_effect=swap):
        with pytest.raises(OSError):
            reference_images._read_image(nested / 'image.png', (root,))
