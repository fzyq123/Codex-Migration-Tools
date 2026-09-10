from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("codex_history_migrator.py")
SPEC = importlib.util.spec_from_file_location("codex_history_migrator", SCRIPT)
assert SPEC and SPEC.loader
migrator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = migrator
SPEC.loader.exec_module(migrator)


class MigratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.codex_home = self.root / ".codex"
        self.sessions = self.codex_home / "sessions" / "2026" / "09" / "09"
        self.sessions.mkdir(parents=True)
        self.thread_id = "01a00000-0000-7000-8000-000000000001"
        self.rollout = self.sessions / f"rollout-2026-09-09T00-00-00-{self.thread_id}.jsonl"
        self.db = self.codex_home / "state_5.sqlite"
        connection = sqlite3.connect(self.db)
        connection.execute(
            """
            CREATE TABLE threads (
              id TEXT PRIMARY KEY, title TEXT, name TEXT, preview TEXT,
              model_provider TEXT,
              rollout_path TEXT, updated_at INTEGER, updated_at_ms INTEGER,
              recency_at INTEGER, recency_at_ms INTEGER, archived INTEGER,
              source TEXT, has_user_event INTEGER
            )
            """
        )
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.thread_id,
                "测试任务",
                None,
                "",
                "OpenAI",
                str(self.rollout),
                1,
                0,
                0,
                0,
                0,
                "vscode",
                1,
            ),
        )
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_rollout(self) -> None:
        rows = [
            {
                "type": "session_meta",
                "payload": {"id": self.thread_id, "model_provider": "OpenAI"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "id": "rs_foreign",
                    "summary": [],
                    "encrypted_content": "foreign",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": "item_foreign",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "保留正文"}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "id": "item_call",
                    "call_id": "call_keep",
                    "name": "tool",
                    "arguments": "{}",
                },
            },
        ]
        self.rollout.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )

    def test_combined_migration_and_idempotence(self) -> None:
        self.write_rollout()
        info = migrator.load_threads(self.db)[0]
        mapping = migrator.map_rollout_files(self.codex_home, {self.thread_id})
        plan = migrator.build_plans([info], mapping, None)[0]
        self.assertEqual(plan.provider_changes, 2)
        self.assertEqual(plan.opaque_items_removed, 1)
        self.assertEqual(plan.portable_ids_cleared, 2)

        backup = self.root / "backups"
        result = migrator.apply_plans(
            [plan], self.db, self.codex_home, backup, None, False
        )
        self.assertIsNotNone(result)
        retained = [json.loads(line) for line in self.rollout.read_text().splitlines()]
        self.assertFalse(
            any(
                row.get("payload", {}).get("type") == "reasoning"
                for row in retained
            )
        )
        message = next(
            row["payload"]
            for row in retained
            if row.get("payload", {}).get("type") == "message"
        )
        call = next(
            row["payload"]
            for row in retained
            if row.get("payload", {}).get("type") == "function_call"
        )
        self.assertNotIn("id", message)
        self.assertEqual(message["content"][0]["text"], "保留正文")
        self.assertNotIn("id", call)
        self.assertEqual(call["call_id"], "call_keep")
        connection = sqlite3.connect(self.db)
        provider = connection.execute(
            "SELECT model_provider FROM threads WHERE id = ?", (self.thread_id,)
        ).fetchone()[0]
        connection.close()
        self.assertEqual(provider, "openai")

        refreshed = migrator.load_threads(self.db)[0]
        second = migrator.build_plans(
            [refreshed],
            migrator.map_rollout_files(self.codex_home, {self.thread_id}),
            None,
        )[0]
        self.assertTrue(second.complete)

    def test_already_openai_with_bad_reasoning_id_is_repaired(self) -> None:
        self.write_rollout()
        connection = sqlite3.connect(self.db)
        connection.execute("UPDATE threads SET model_provider='openai'")
        connection.commit()
        connection.close()
        rows = [json.loads(line) for line in self.rollout.read_text().splitlines()]
        rows[0]["payload"]["model_provider"] = "openai"
        rows[1]["payload"]["id"] = "item_bad_reasoning"
        self.rollout.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        info = migrator.load_threads(self.db)[0]
        plan = migrator.build_plans(
            [info],
            migrator.map_rollout_files(self.codex_home, {self.thread_id}),
            None,
        )[0]
        self.assertEqual(plan.provider_changes, 0)
        self.assertEqual(plan.opaque_items_removed, 1)

    def test_force_drop_opaque_for_legacy_provider_only_migration(self) -> None:
        self.write_rollout()
        connection = sqlite3.connect(self.db)
        connection.execute("UPDATE threads SET model_provider='openai'")
        connection.commit()
        connection.close()
        rows = [json.loads(line) for line in self.rollout.read_text().splitlines()]
        rows[0]["payload"]["model_provider"] = "openai"
        self.rollout.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        info = migrator.load_threads(self.db)[0]
        mapping = migrator.map_rollout_files(self.codex_home, {self.thread_id})
        automatic = migrator.build_plans([info], mapping, None, False)[0]
        forced = migrator.build_plans([info], mapping, None, True)[0]
        self.assertEqual(automatic.opaque_items_removed, 0)
        self.assertEqual(forced.opaque_items_removed, 1)

    def test_complete_task_does_not_block_pending_batch(self) -> None:
        self.write_rollout()
        info = migrator.load_threads(self.db)[0]
        mapping = migrator.map_rollout_files(self.codex_home, {self.thread_id})
        pending = migrator.build_plans([info], mapping, None)[0]
        complete_info = migrator.ThreadInfo(
            thread_id="01a00000-0000-7000-8000-000000000099",
            title="无需处理",
            provider="openai",
            rollout_path=self.root / "missing.jsonl",
            updated_at_ms=0,
            archived=False,
            source="vscode",
            has_user_event=False,
        )
        complete = migrator.ThreadPlan(
            info=complete_info, files=[], db_provider_after="openai"
        )
        # The completed task is not present in SQLite. apply_plans succeeds only
        # if it refreshes/locks pending tasks rather than every selected task.
        result = migrator.apply_plans(
            [complete, pending],
            self.db,
            self.codex_home,
            self.root / "backups",
            None,
            False,
        )
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
