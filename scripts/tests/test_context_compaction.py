"""Check compaction migration safety without requiring a running Open WebUI."""
import asyncio
import copy
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'config/openwebui'))
import apply_context_compaction as compaction
import apply_model_parameters as presets


class Model(SimpleNamespace):
    def model_dump(self, exclude=()):
        return {k: copy.deepcopy(v) for k, v in vars(self).items() if k not in exclude}


class Store:
    def __init__(self):
        self.models = {
            'Small': Model(id='Small', base_model_id=None, params={'temperature': 0.4}, meta={}, access_grants=['keep']),
            'ai-stack-image-generation': Model(
                id='ai-stack-image-generation', base_model_id='Small',
                params={'system': 'edited prompt', 'custom_params': {'cache_prompt': True}},
                meta={'ai_stack_managed_by': presets.MANAGED_BY}, access_grants=['keep']),
        }
        self.values = {'task.model.params': {'temperature': 0.3, 'custom_params': {'seed': 42}}}
        self.writes = 0
        self.fail_write = False

    async def get_model_by_id(self, model_id):
        return self.models.get(model_id)

    async def update_model_by_id(self, model_id, form):
        self.writes += 1
        if not self.fail_write:
            self.models[model_id] = Model(**vars(form), access_grants=self.models[model_id].access_grants)

    async def get(self, key):
        return self.values.get(key)

    async def upsert(self, values):
        self.values.update(copy.deepcopy(values))


class CompactionTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = Store()

    def apply(self):
        asyncio.run(compaction.apply_compaction(self.store, Model, self.store))

    def test_preserves_edits_grants_and_task_options(self):
        self.apply()
        image = self.store.models['ai-stack-image-generation']
        self.assertEqual(image.params['system'], 'edited prompt')
        self.assertEqual(image.access_grants, ['keep'])
        self.assertTrue(image.params['custom_params']['cache_prompt'])
        self.assertEqual(image.params['compact_token_threshold'], 8192)
        self.assertEqual(self.store.models['Small'].params['temperature'], 0.4)
        self.assertEqual(self.store.values['task.model.params'],
                         {'temperature': 0.3, 'custom_params': {'seed': 42}, 'max_tokens': 2048})
        self.assertTrue(self.store.values[compaction.COMPACTION_KEY])
        self.assertEqual(self.store.values['chat.context_compaction.model'], 'Big')

    def test_unrelated_preset_rejected_before_writes(self):
        self.store.models['ai-stack-image-generation'].meta = {}
        with self.assertRaises(RuntimeError):
            self.apply()
        self.assertEqual(self.store.writes, 0)
        self.assertNotIn(compaction.COMPACTION_KEY, self.store.values)

    def test_failed_write_does_not_complete_and_can_retry(self):
        self.store.fail_write = True
        with self.assertRaises(RuntimeError):
            self.apply()
        self.assertNotIn(compaction.COMPACTION_KEY, self.store.values)
        self.store.fail_write = False
        self.apply()
        self.assertTrue(self.store.values[compaction.COMPACTION_KEY])

    def test_invalid_budget_rejected_before_writes(self):
        os.environ['CONTEXT_COMPACTION_TOKEN_CAP'] = '32768'
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual(self.store.writes, 0)

    def test_explicit_environment_changes(self):
        os.environ.update(ENABLE_CONTEXT_COMPACTION='false',
                          CONTEXT_COMPACTION_TOKEN_THRESHOLD='32768',
                          CONTEXT_COMPACTION_TOKEN_CAP='32768',
                          CONTEXT_COMPACTION_RETENTION_PERCENTAGE='30',
                          OPENWEBUI_SMALL_COMPACTION_THRESHOLD='4096')
        self.apply()
        self.assertFalse(self.store.values['chat.context_compaction.enable'])
        self.assertEqual(self.store.values['chat.context_compaction.token_threshold'], 32768)
        self.assertEqual(self.store.models['Small'].params['compact_token_threshold'], 4096)

    def test_upgrade_runs_pending_migration(self):
        for key in (presets.BOOTSTRAP_KEY, presets.PUBLIC_ACCESS_KEY, presets.PROMPT_CACHE_KEY,
                    presets.ALLROUND_WEB_SEARCH_KEY, presets.IMAGE_GENERATION_KEY,
                    presets.CODING_TERMINAL_KEY, presets.CHAT_MODEL_KEY):
            self.store.values[key] = True
        users = SimpleNamespace(get_first_user=lambda: SimpleNamespace(role='admin'))
        self.assertTrue(asyncio.run(presets.bootstrap_once(self.store, users, Model, self.store)))
        self.assertTrue(self.store.values[compaction.COMPACTION_KEY])
        self.assertEqual(self.store.writes, 2)

    def test_config_write_failure_remains_pending(self):
        async def ignore_write(values):
            pass
        self.store.upsert = ignore_write
        with self.assertRaises(RuntimeError):
            self.apply()
        self.assertNotIn(compaction.COMPACTION_KEY, self.store.values)

    def test_restart_preserves_later_admin_edits(self):
        for key in (presets.BOOTSTRAP_KEY, presets.PUBLIC_ACCESS_KEY, presets.PROMPT_CACHE_KEY,
                    presets.ALLROUND_WEB_SEARCH_KEY, presets.IMAGE_GENERATION_KEY,
                    presets.CODING_TERMINAL_KEY, presets.CHAT_MODEL_KEY, compaction.COMPACTION_KEY):
            self.store.values[key] = True
        self.store.values['chat.context_compaction.enable'] = False
        result = asyncio.run(presets.bootstrap_once(self.store, None, Model, self.store))
        self.assertTrue(result)
        self.assertEqual(self.store.writes, 0)
        self.assertFalse(self.store.values['chat.context_compaction.enable'])


if __name__ == '__main__':
    unittest.main()
