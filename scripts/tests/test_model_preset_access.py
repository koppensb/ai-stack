"""Regression tests for model sharing and existing-installation migration."""
import asyncio
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

PATH = Path(__file__).resolve().parents[2] / 'config/openwebui/apply_model_parameters.py'
spec = importlib.util.spec_from_file_location('presets', PATH)
presets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(presets)


# Lightweight store doubles keep these tests independent of Docker/Open WebUI.
# They test migration behavior, not live API compatibility or UI permissions.
class Record(SimpleNamespace):
    def model_dump(self, exclude=()):
        return {k: copy.deepcopy(v) for k, v in vars(self).items() if k not in exclude}


class Models:
    def __init__(self):
        self.rows = {}

    def get_model_by_id(self, key):
        return self.rows.get(key)

    def insert_new_model(self, form, owner):
        row = Record(**form.model_dump(), user_id=owner)
        self.rows[row.id] = row
        return row

    def update_model_by_id(self, key, form):
        self.rows[key] = form
        return form


# The model store above is synchronous while grant/config stores are asynchronous;
# the mixed interface exercises the compatibility resolver used in production.
class Grants:
    def __init__(self):
        self.rows = {}
        self.fail_id = None

    async def grant_access(self, resource_type, resource_id, **grant):
        assert resource_type == 'model'
        if resource_id == self.fail_id:
            return None
        values = self.rows.setdefault(resource_id, [])
        if grant not in values:
            values.append(grant)
        return grant

    async def get_grants_by_resource(self, resource_type, resource_id):
        return [Record(**v) for v in self.rows.get(resource_id, [])]


class Config:
    def __init__(self, values=None):
        self.values = dict(values or {})

    async def get(self, key):
        return self.values.get(key)

    async def upsert(self, values):
        self.values.update(values)


class PublicAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.models, self.grants = Models(), Grants()
        self.users = SimpleNamespace(get_first_user=lambda: Record(id='admin', role='admin'))
        self.config = Config()

    async def apply(self):
        await presets.apply_names(self.models, self.users, Record, grants=self.grants)

    async def boot(self):
        return await presets.bootstrap_once(
            self.models, self.users, Record, self.config, self.grants)

    async def test_fresh_install_and_idempotence(self):
        self.assertTrue(await self.boot())
        ids = set(presets.MODEL_PRESETS) | {base for _, base in presets.MODEL_PRESETS.values()}
        self.assertEqual(set(self.grants.rows), ids)
        self.assertEqual(set(self.models.rows), ids)
        for grants in self.grants.rows.values():
            self.assertEqual(grants, [dict(principal_type='user', principal_id='*', permission='read')])
        self.assertTrue(self.config.values[presets.PROMPT_CACHE_KEY])
        self.assertTrue(self.config.values[presets.ALLROUND_WEB_SEARCH_KEY])
        for key in presets.MODEL_PRESETS:
            meta = self.models.rows[key].meta
            self.assertIs(meta['capabilities']['terminal'], key == 'ai-stack-coding')
            self.assertEqual(meta.get('terminalId'),
                             presets.TERMINAL_ID if key == 'ai-stack-coding' else None)
            self.assertEqual('web_search' in meta.get('defaultFeatureIds', []),
                             key == 'ai-stack-allround')
            self.assertEqual('image_generation' in meta.get('defaultFeatureIds', []),
                             key == 'ai-stack-image-generation')
        for model_id in presets.MODEL_PRESETS:
            self.assertTrue(presets.prompt_cache_enabled(self.models.rows[model_id]))
        before = copy.deepcopy(self.grants.rows)
        await self.apply()
        self.assertEqual(before, self.grants.rows)
        for _, base in presets.MODEL_PRESETS.values():
            self.assertEqual(self.models.rows[base].params, {})

    async def test_migration_preserves_edits_and_existing_grants(self):
        await self.apply()
        self.config.values = {presets.BOOTSTRAP_KEY: True, presets.PROMPT_CACHE_KEY: True}
        self.models.rows['ai-stack-coding'].params = {'system': 'My prompt', 'temperature': 0.12}
        base_id = presets.MODEL_PRESETS['ai-stack-coding'][1]
        self.models.rows[base_id].params = {'num_ctx': 4096}
        self.models.rows[base_id].name = 'Existing base name'
        self.grants.rows = {base_id: [dict(principal_type='group', principal_id='editors', permission='write')]}
        original = copy.deepcopy(self.models.rows)
        await self.boot()
        self.assertEqual(original, self.models.rows)
        self.assertEqual(len(self.grants.rows[base_id]), 2)
        self.assertTrue(self.config.values[presets.PUBLIC_ACCESS_KEY])
        # Completed startup does not reapply later administrator edits.
        self.grants.rows = {}
        await self.boot()
        self.assertEqual(self.grants.rows, {})

    async def test_failed_grant_does_not_complete_migration_and_can_retry(self):
        await self.apply()
        self.config.values = {presets.BOOTSTRAP_KEY: True, presets.PROMPT_CACHE_KEY: True}
        self.grants.rows = {}
        self.grants.fail_id = 'ai-stack-allround'
        with self.assertRaisesRegex(RuntimeError, 'Failed to grant'):
            await self.boot()
        self.assertNotIn(presets.PUBLIC_ACCESS_KEY, self.config.values)
        self.grants.fail_id = None
        await self.boot()
        self.assertTrue(self.config.values[presets.PUBLIC_ACCESS_KEY])

    async def test_unrelated_preset_is_not_shared(self):
        await self.apply()
        self.config.values = {presets.BOOTSTRAP_KEY: True, presets.PROMPT_CACHE_KEY: True}
        self.grants.rows = {}
        self.models.rows['ai-stack-creativ'].meta = {}
        with self.assertRaisesRegex(RuntimeError, 'unrelated model'):
            await self.boot()
        self.assertEqual(self.grants.rows, {})

    async def test_chat_migration_preserves_edits_and_image_model(self):
        await self.boot()
        self.config.values.pop(presets.CHAT_MODEL_KEY)
        for key in ('ai-stack-allround', 'ai-stack-creativ'):
            self.models.rows[key].base_model_id = 'previous-chat-model'
            self.models.rows[key].params = {'system': 'Custom prompt', 'temperature': 0.12}
            self.models.rows[key].name = 'Custom name'
        before = copy.deepcopy(self.models.rows)
        grants = copy.deepcopy(self.grants.rows)
        await self.boot()
        for key in ('ai-stack-allround', 'ai-stack-creativ'):
            before[key].base_model_id = 'Qwen3.8-27B'
        self.assertEqual(self.models.rows, before)
        self.assertEqual(self.grants.rows, grants)
        self.assertTrue(self.config.values[presets.CHAT_MODEL_KEY])
        await self.boot()
        self.assertEqual(self.models.rows, before)

    async def test_chat_migration_failure_retries(self):
        await self.boot()
        self.config.values.pop(presets.CHAT_MODEL_KEY)
        self.models.rows['ai-stack-allround'].base_model_id = 'previous-chat-model'
        update = self.models.update_model_by_id
        self.models.update_model_by_id = lambda key, form: None
        with self.assertRaisesRegex(RuntimeError, 'Failed to retarget'):
            await self.boot()
        self.assertNotIn(presets.CHAT_MODEL_KEY, self.config.values)
        self.models.update_model_by_id = update
        await self.boot()
        self.assertTrue(self.config.values[presets.CHAT_MODEL_KEY])

    async def test_chat_migration_validates_before_writing(self):
        await self.boot()
        self.config.values.pop(presets.CHAT_MODEL_KEY)
        self.models.rows['ai-stack-allround'].base_model_id = 'previous-chat-model'
        self.models.rows['ai-stack-creativ'].meta = {}
        before = copy.deepcopy(self.models.rows)
        with self.assertRaisesRegex(RuntimeError, 'unrelated model'):
            await self.boot()
        self.assertEqual(self.models.rows, before)
        self.assertNotIn(presets.CHAT_MODEL_KEY, self.config.values)

    async def test_chat_migration_waits_for_shared_base_access(self):
        await self.boot()
        self.config.values.pop(presets.CHAT_MODEL_KEY)
        self.models.rows['ai-stack-allround'].base_model_id = 'previous-chat-model'
        self.grants.rows.pop('Qwen3.8-27B')
        self.grants.fail_id = 'Qwen3.8-27B'
        with self.assertRaisesRegex(RuntimeError, 'Failed to grant'):
            await self.boot()
        self.assertNotIn(presets.CHAT_MODEL_KEY, self.config.values)
        self.grants.fail_id = None
        await self.boot()
        self.assertTrue(self.config.values[presets.CHAT_MODEL_KEY])
        self.assertTrue(self.grants.rows['Qwen3.8-27B'])

    def test_download_plan_matches_shared_chat_and_image_presets(self):
        root = PATH.parents[2]
        spec = importlib.util.spec_from_file_location('downloads', root / 'scripts/download_models.py')
        downloads = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(downloads)
        chat = downloads.chat_plan(root / 'config/llama-cpp/models.ini')
        image = downloads.chat_plan(root / 'config/llama-cpp-image/models.ini')
        self.assertEqual([name for name, _ in chat], ['Qwen3.8-27B'])
        self.assertEqual([name for name, _ in image], ['Qwen3.5-4B'])
        for role, base in presets.MODEL_PRESETS.values():
            self.assertEqual(base, image[0][0] if role == 'Image Generation' else chat[0][0])

    async def test_manual_reapply_retargets_existing_chat_roles(self):
        await self.apply()
        self.models.rows['ai-stack-allround'].base_model_id = 'previous-chat-model'
        await self.apply()
        self.assertEqual(self.models.rows['ai-stack-allround'].base_model_id, 'Qwen3.8-27B')

    async def test_waits_for_admin(self):
        self.users.get_first_user = lambda: None
        self.assertFalse(await self.boot())
        self.assertEqual(self.models.rows, {})
        self.assertEqual(self.config.values, {})

    async def test_async_model_store(self):
        original = self.models
        async def get(key):
            return original.get_model_by_id(key)
        async def insert(form, owner):
            return original.insert_new_model(form, owner)
        async def update(key, form):
            return original.update_model_by_id(key, form)
        self.models = SimpleNamespace(get_model_by_id=get, insert_new_model=insert,
                                      update_model_by_id=update)
        self.assertTrue(await self.boot())
        self.assertEqual(len(original.rows), 6)


    async def test_cache_migration_preserves_customizations_and_grants(self):
        await self.apply()
        self.config.values = {presets.BOOTSTRAP_KEY: True, presets.PUBLIC_ACCESS_KEY: True}
        for key in presets.MODEL_PRESETS:
            self.models.rows[key].params = {
                'system': 'User prompt', 'temperature': 0.13, 'cache_prompt': False,
                'custom_params': {'cache_prompt': 'false', 'seed': 42},
            }
            self.models.rows[key].name = 'Custom name'
        original = copy.deepcopy(self.models.rows)
        grants = copy.deepcopy(self.grants.rows)
        await self.boot()
        for key in presets.MODEL_PRESETS:
            original[key].params.pop('cache_prompt')
            original[key].params['custom_params']['cache_prompt'] = True
        self.assertEqual(self.models.rows, original)
        self.assertEqual(self.grants.rows, grants)
        self.assertTrue(self.config.values[presets.PROMPT_CACHE_KEY])
        # A later manual override is retained on ordinary startup.
        self.models.rows['ai-stack-coding'].params['custom_params']['cache_prompt'] = False
        await self.boot()
        self.assertFalse(presets.prompt_cache_enabled(self.models.rows['ai-stack-coding']))

    async def test_cache_migration_failure_retries_without_completion_marker(self):
        await self.apply()
        self.config.values = {presets.BOOTSTRAP_KEY: True, presets.PUBLIC_ACCESS_KEY: True}
        for key in presets.MODEL_PRESETS:
            self.models.rows[key].params['custom_params']['cache_prompt'] = False
        update = self.models.update_model_by_id
        self.models.update_model_by_id = lambda key, form: None
        with self.assertRaisesRegex(RuntimeError, 'Failed to enable prompt caching'):
            await self.boot()
        self.assertNotIn(presets.PROMPT_CACHE_KEY, self.config.values)
        self.models.update_model_by_id = update
        await self.boot()
        self.assertTrue(self.config.values[presets.PROMPT_CACHE_KEY])

    async def test_cache_migration_validates_all_targets_before_writing(self):
        await self.apply()
        self.config.values = {presets.BOOTSTRAP_KEY: True, presets.PUBLIC_ACCESS_KEY: True}
        self.models.rows['ai-stack-creativ'].meta = {}
        original = copy.deepcopy(self.models.rows)
        with self.assertRaisesRegex(RuntimeError, 'unrelated model'):
            await self.boot()
        self.assertEqual(original, self.models.rows)
        self.assertNotIn(presets.PROMPT_CACHE_KEY, self.config.values)

    async def test_allround_search_migration_preserves_other_settings(self):
        await self.boot()
        self.config.values.pop(presets.ALLROUND_WEB_SEARCH_KEY)
        model = self.models.rows['ai-stack-allround']
        model.params = {'system': 'Custom prompt', 'temperature': 0.12}
        model.meta.update(defaultFeatureIds=['code_interpreter'],
                          capabilities={'web_search': False, 'vision': False})
        before = copy.deepcopy(self.models.rows)
        grants = copy.deepcopy(self.grants.rows)
        await self.boot()
        before['ai-stack-allround'].meta['defaultFeatureIds'].append('web_search')
        before['ai-stack-allround'].meta['capabilities']['web_search'] = True
        self.assertEqual(self.models.rows, before)
        self.assertEqual(self.grants.rows, grants)
        self.assertTrue(self.config.values[presets.ALLROUND_WEB_SEARCH_KEY])
        self.models.rows['ai-stack-allround'].meta['defaultFeatureIds'] = []
        await self.boot()
        self.assertEqual(self.models.rows['ai-stack-allround'].meta['defaultFeatureIds'], [])

    async def test_allround_search_failure_retries(self):
        await self.boot()
        self.config.values.pop(presets.ALLROUND_WEB_SEARCH_KEY)
        self.models.rows['ai-stack-allround'].meta['defaultFeatureIds'] = []
        update = self.models.update_model_by_id
        self.models.update_model_by_id = lambda key, form: None
        with self.assertRaisesRegex(RuntimeError, 'Failed to enable Allround'):
            await self.boot()
        self.assertNotIn(presets.ALLROUND_WEB_SEARCH_KEY, self.config.values)
        self.models.update_model_by_id = update
        await self.boot()
        self.assertTrue(self.config.values[presets.ALLROUND_WEB_SEARCH_KEY])

    async def test_allround_search_rejects_unmanaged_model(self):
        await self.boot()
        self.config.values.pop(presets.ALLROUND_WEB_SEARCH_KEY)
        self.models.rows['ai-stack-allround'].meta = {}
        before = copy.deepcopy(self.models.rows)
        with self.assertRaisesRegex(RuntimeError, 'unrelated model'):
            await self.boot()
        self.assertEqual(self.models.rows, before)
        self.assertNotIn(presets.ALLROUND_WEB_SEARCH_KEY, self.config.values)

    async def test_image_generation_migration_preserves_other_settings(self):
        await self.boot()
        self.config.values.pop(presets.IMAGE_GENERATION_KEY)
        model = self.models.rows['ai-stack-image-generation']
        model.params = {'system': 'Custom prompt', 'temperature': 0.12}
        model.meta.update(defaultFeatureIds=['code_interpreter'],
                          capabilities={'image_generation': False, 'vision': False})
        before = copy.deepcopy(self.models.rows)
        grants = copy.deepcopy(self.grants.rows)
        await self.boot()
        before['ai-stack-image-generation'].meta['defaultFeatureIds'].append('image_generation')
        before['ai-stack-image-generation'].meta['capabilities']['image_generation'] = True
        self.assertEqual(self.models.rows, before)
        self.assertEqual(self.grants.rows, grants)
        self.assertTrue(self.config.values[presets.IMAGE_GENERATION_KEY])
        self.models.rows['ai-stack-image-generation'].meta['defaultFeatureIds'] = []
        await self.boot()
        self.assertEqual(self.models.rows['ai-stack-image-generation'].meta['defaultFeatureIds'], [])

    async def test_image_generation_failure_retries(self):
        await self.boot()
        self.config.values.pop(presets.IMAGE_GENERATION_KEY)
        self.models.rows['ai-stack-image-generation'].meta['defaultFeatureIds'] = []
        update = self.models.update_model_by_id
        self.models.update_model_by_id = lambda key, form: None
        with self.assertRaisesRegex(RuntimeError, 'Failed to enable Image Generation'):
            await self.boot()
        self.assertNotIn(presets.IMAGE_GENERATION_KEY, self.config.values)
        self.models.update_model_by_id = update
        await self.boot()
        self.assertTrue(self.config.values[presets.IMAGE_GENERATION_KEY])

    async def test_image_generation_rejects_unmanaged_model(self):
        await self.boot()
        self.config.values.pop(presets.IMAGE_GENERATION_KEY)
        self.models.rows['ai-stack-image-generation'].meta = {}
        before = copy.deepcopy(self.models.rows)
        with self.assertRaisesRegex(RuntimeError, 'unrelated model'):
            await self.boot()
        self.assertEqual(self.models.rows, before)
        self.assertNotIn(presets.IMAGE_GENERATION_KEY, self.config.values)

    async def test_terminal_migration_preserves_other_settings(self):
        await self.boot()
        self.config.values.pop(presets.CODING_TERMINAL_KEY)
        for key in presets.MODEL_PRESETS:
            row = self.models.rows[key]
            row.params = {'system': 'Custom prompt', 'temperature': 0.12}
            row.meta['capabilities']['terminal'] = key != 'ai-stack-coding'
            row.meta['terminalId'] = 'previous-terminal'
        before = copy.deepcopy(self.models.rows)
        grants = copy.deepcopy(self.grants.rows)
        await self.boot()
        for key in presets.MODEL_PRESETS:
            enabled = key == 'ai-stack-coding'
            before[key].meta['capabilities']['terminal'] = enabled
            if enabled:
                before[key].meta['terminalId'] = presets.TERMINAL_ID
            else:
                before[key].meta.pop('terminalId')
        self.assertEqual(self.models.rows, before)
        self.assertEqual(self.grants.rows, grants)
        self.assertTrue(self.config.values[presets.CODING_TERMINAL_KEY])
        await self.boot()
        self.assertEqual(self.models.rows, before)

    async def test_terminal_migration_failure_can_retry(self):
        await self.boot()
        self.config.values.pop(presets.CODING_TERMINAL_KEY)
        self.models.rows['ai-stack-coding'].meta['terminalId'] = 'wrong-terminal'
        update = self.models.update_model_by_id
        self.models.update_model_by_id = lambda key, form: None
        with self.assertRaisesRegex(RuntimeError, 'Failed to configure terminal'):
            await self.boot()
        self.assertNotIn(presets.CODING_TERMINAL_KEY, self.config.values)
        self.models.update_model_by_id = update
        await self.boot()
        self.assertTrue(self.config.values[presets.CODING_TERMINAL_KEY])

    async def test_terminal_migration_validates_all_before_writing(self):
        await self.boot()
        self.config.values.pop(presets.CODING_TERMINAL_KEY)
        self.models.rows['ai-stack-image-generation'].meta = {}
        self.models.rows['ai-stack-coding'].meta['terminalId'] = 'previous-terminal'
        before = copy.deepcopy(self.models.rows)
        with self.assertRaisesRegex(RuntimeError, 'unrelated model'):
            await self.boot()
        self.assertEqual(self.models.rows, before)
        self.assertNotIn(presets.CODING_TERMINAL_KEY, self.config.values)

    def test_cache_params_copy_and_validate_custom_settings(self):
        original = {'custom_params': {'seed': 42, 'cache_prompt': False}, 'num_predict': 512}
        result = presets.prompt_cache_params(original)
        self.assertFalse(original['custom_params']['cache_prompt'])
        self.assertEqual(result['custom_params'], {'seed': 42, 'cache_prompt': True})
        self.assertEqual(result['num_predict'], 512)
        self.assertEqual(result, presets.prompt_cache_params(result))
        with self.assertRaises(ValueError):
            presets.prompt_cache_params({'custom_params': 'not a mapping'})


if __name__ == '__main__':
    unittest.main()
