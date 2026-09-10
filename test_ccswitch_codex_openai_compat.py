from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("ccswitch_codex_openai_compat.py")
SPEC = importlib.util.spec_from_file_location("ccswitch_codex_openai_compat", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


CUSTOM_CONFIG = '''model_provider = "OpenAI"
model = "gpt-test"

[model_providers.OpenAI]
name = "Proxy"
base_url = "https://proxy.example/v1"
wire_api = "responses"
requires_openai_auth = false

[desktop]
followUpQueueMode = "queue"
'''


def make_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        '''
        CREATE TABLE providers (
            id TEXT NOT NULL,
            app_type TEXT NOT NULL,
            name TEXT NOT NULL,
            settings_config TEXT NOT NULL,
            meta TEXT NOT NULL DEFAULT '{}',
            is_current BOOLEAN NOT NULL DEFAULT 0,
            sort_index INTEGER,
            PRIMARY KEY (id, app_type)
        )
        '''
    )
    connection.executemany(
        "INSERT INTO providers "
        "(id, app_type, name, settings_config, meta, is_current, sort_index) "
        "VALUES (?, 'codex', ?, ?, ?, ?, ?)",
        [
            (
                "codex-official",
                "OpenAI Official",
                json.dumps(
                    {
                        "auth": {
                            "auth_mode": "chatgpt",
                            "OPENAI_API_KEY": None,
                            "tokens": {"access_token": "oauth-secret"},
                        },
                        "config": 'model = "gpt-test"\n',
                    }
                ),
                "{}",
                1,
                0,
            ),
            (
                "proxy-id",
                "proxy",
                json.dumps(
                    {
                        "auth": {"OPENAI_API_KEY": "secret-value"},
                        "config": CUSTOM_CONFIG,
                        "modelCatalog": {
                            "models": [
                                {
                                    "model": "third-party-model",
                                    "displayName": "Third Party Model",
                                    "contextWindow": 200000,
                                }
                            ]
                        },
                    },
                    ensure_ascii=False,
                ),
                '{"apiFormat":"openai_responses"}',
                0,
                1,
            ),
        ],
    )
    connection.commit()
    connection.close()


class ConfigConversionTests(unittest.TestCase):
    def test_converts_custom_provider_and_preserves_other_tables(self) -> None:
        rewritten, before = module.convert_config(CUSTOM_CONFIG)
        self.assertEqual(before.active_provider, "OpenAI")
        after = module.analyze_config(rewritten)
        self.assertEqual(after.active_provider, "openai")
        self.assertEqual(after.openai_base_url, "https://proxy.example/v1")
        self.assertNotIn("OpenAI", after.provider_tables)
        self.assertIn("[desktop]", rewritten)
        self.assertIn('model = "gpt-test"', rewritten)

    def test_already_compatible_is_idempotent(self) -> None:
        config = (
            'model_provider = "openai"\n'
            'openai_base_url = "https://proxy.example/v1"\n'
        )
        rewritten, _ = module.convert_config(config)
        self.assertEqual(rewritten, config)

    def test_removes_stale_catalog_reference_without_catalog(self) -> None:
        config = (
            'model_provider = "openai"\n'
            'openai_base_url = "https://proxy.example/v1"\n'
            'model_catalog_json = "cc-switch-model-catalog.json"\n'
        )
        rewritten = module.configure_model_catalog_reference(config, None)
        self.assertNotIn("model_catalog_json", rewritten)

    def test_rejects_provider_specific_headers(self) -> None:
        config = CUSTOM_CONFIG.replace(
            'wire_api = "responses"',
            'wire_api = "responses"\nhttp_headers = { X-Test = "yes"}',
        )
        with self.assertRaisesRegex(module.ConversionError, "无法安全解析"):
            module.convert_config(config)

    def test_rejects_nested_auth_table(self) -> None:
        config = CUSTOM_CONFIG + '''
[model_providers.OpenAI.auth]
command = "/tmp/token"
'''
        with self.assertRaisesRegex(module.ConversionError, "嵌套认证表"):
            module.convert_config(config)


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "cc-switch.db"
        make_db(self.db)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_apply_backs_up_and_preserves_auth(self) -> None:
        records = module.load_records(self.db)
        selected = module.select_records(records, ["proxy"], False)
        plans = [module.build_plan(record) for record in selected]
        backup = module.apply_plans(self.db, self.root / "backups", plans)
        self.assertIsNotNone(backup)
        self.assertTrue((backup / "cc-switch.db.snapshot").exists())
        self.assertTrue((backup / "manifest.json").exists())

        updated = module.load_records(self.db)
        proxy = next(record for record in updated if record.name == "proxy")
        self.assertEqual(proxy.settings["auth"]["OPENAI_API_KEY"], "secret-value")
        analysis = module.analyze_config(proxy.config_text)
        self.assertEqual(analysis.active_provider, "openai")
        self.assertEqual(analysis.openai_base_url, "https://proxy.example/v1")

        snapshot_records = module.load_records(backup / "cc-switch.db.snapshot")
        snapshot_proxy = next(record for record in snapshot_records if record.name == "proxy")
        self.assertEqual(snapshot_proxy.config_text, CUSTOM_CONFIG)

    def test_refuses_official_selection(self) -> None:
        records = module.load_records(self.db)
        with self.assertRaisesRegex(RuntimeError, "拒绝修改"):
            module.select_records(records, ["OpenAI Official"], False)

    def test_all_third_party_excludes_official(self) -> None:
        records = module.load_records(self.db)
        selected = module.select_records(records, None, True)
        self.assertEqual([record.name for record in selected], ["proxy"])

    def test_plan_uses_meta_api_format_instead_of_wire_api(self) -> None:
        connection = sqlite3.connect(self.db)
        connection.execute(
            "UPDATE providers SET meta = ? WHERE id = 'proxy-id'",
            ('{"apiFormat":"openai_chat"}',),
        )
        connection.commit()
        connection.close()

        record = next(item for item in module.load_records(self.db) if item.name == "proxy")
        plan = module.build_plan(record)
        self.assertIn("OpenAI Chat Completions", plan.error or "")

    def test_plan_rejects_missing_upstream_api_format(self) -> None:
        connection = sqlite3.connect(self.db)
        connection.execute("UPDATE providers SET meta = '{}' WHERE id = 'proxy-id'")
        connection.commit()
        connection.close()

        record = next(item for item in module.load_records(self.db) if item.name == "proxy")
        plan = module.build_plan(record)
        self.assertIn("meta.apiFormat 缺失", plan.error or "")

    def test_compatible_activation_backs_up_live_files(self) -> None:
        codex_home = self.root / "codex"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text('model_provider = "old"\n')
        (codex_home / "auth.json").write_text('{"old": true}\n')

        records = module.load_records(self.db)
        record = next(item for item in records if item.name == "proxy")
        plan = module.build_plan(record)
        backup = module.activate_record(
            self.db, self.root / "backups", codex_home, record, plan
        )

        config = (codex_home / "config.toml").read_text()
        self.assertIn('model_provider = "openai"', config)
        self.assertIn('openai_base_url = "https://proxy.example/v1"', config)
        self.assertIn(
            'model_catalog_json = "cc-switch-model-catalog.json"', config
        )
        self.assertEqual(
            json.loads((codex_home / "auth.json").read_text()),
            {"OPENAI_API_KEY": "secret-value"},
        )
        catalog = json.loads(
            (codex_home / "cc-switch-model-catalog.json").read_text()
        )
        self.assertEqual(catalog["models"][0]["slug"], "third-party-model")
        self.assertEqual(catalog["models"][0]["display_name"], "Third Party Model")
        self.assertEqual(catalog["models"][0]["context_window"], 200000)
        self.assertEqual(
            (backup / "codex-live" / "config.toml").read_text(),
            'model_provider = "old"\n',
        )
        current = next(item for item in module.load_records(self.db) if item.is_current)
        self.assertEqual(current.name, "proxy")

    def test_official_activation_restores_chatgpt_auth(self) -> None:
        codex_home = self.root / "codex"
        codex_home.mkdir()
        catalog_path = codex_home / "cc-switch-model-catalog.json"
        catalog_path.write_text('{"models": [{"slug": "stale"}]}\n')
        records = module.load_records(self.db)
        record = next(item for item in records if item.name == "OpenAI Official")
        plan = module.build_plan(record)
        module.activate_record(
            self.db, self.root / "backups", codex_home, record, plan
        )
        auth = json.loads((codex_home / "auth.json").read_text())
        self.assertEqual(auth["auth_mode"], "chatgpt")
        self.assertIn("tokens", auth)
        self.assertFalse(catalog_path.exists())


if __name__ == "__main__":
    unittest.main()
