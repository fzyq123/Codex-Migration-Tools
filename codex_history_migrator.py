#!/usr/bin/env python3
"""Migrate Codex task history to the built-in ``openai`` provider bucket.

This tool combines the provider alignment strategy from
``makoMakoGo/fish-claude/tools/codex-provider-history-migrator`` with a
cross-provider Responses item compatibility pass. It never edits config.toml.

Default mode is read-only. Pass --apply to write after reviewing the preview.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Iterator


TARGET_PROVIDER = "openai"
UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# Type-specific output item prefixes enforced by the OpenAI Responses API.
# Only types whose IDs are optional in Codex history are sanitized here.
PORTABLE_ID_PREFIXES = {
    "message": "msg_",
    "agent_message": "amsg_",
    "function_call": "fc_",
    "function_call_output": "fco_",
    "custom_tool_call": "ctc_",
    "custom_tool_call_output": "ctco_",
}
OPAQUE_ID_PREFIXES = {
    "reasoning": "rs_",
    "compaction": "cmp_",
}


@dataclass(frozen=True)
class ThreadInfo:
    thread_id: str
    title: str
    provider: str
    rollout_path: Path
    updated_at_ms: int
    archived: bool
    source: str
    has_user_event: bool


@dataclass
class FilePlan:
    path: Path
    original: bytes
    rewritten: bytes
    providers_before: set[str] = field(default_factory=set)
    providers_after: set[str] = field(default_factory=set)
    session_meta_rewrites: int = 0
    opaque_items_removed: int = 0
    portable_ids_cleared: int = 0
    parse_errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.original != self.rewritten


@dataclass
class ThreadPlan:
    info: ThreadInfo
    files: list[FilePlan]
    db_provider_after: str

    @property
    def db_needs_update(self) -> bool:
        return self.info.provider != self.db_provider_after

    @property
    def provider_changes(self) -> int:
        return int(self.db_needs_update) + sum(
            item.session_meta_rewrites for item in self.files
        )

    @property
    def opaque_items_removed(self) -> int:
        return sum(item.opaque_items_removed for item in self.files)

    @property
    def portable_ids_cleared(self) -> int:
        return sum(item.portable_ids_cleared for item in self.files)

    @property
    def parse_errors(self) -> list[str]:
        return [error for item in self.files for error in item.parse_errors]

    @property
    def changed_files(self) -> list[FilePlan]:
        return [item for item in self.files if item.changed]

    @property
    def complete(self) -> bool:
        return not self.db_needs_update and not self.changed_files


def default_codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".codex"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "将指定 Codex 任务迁移到 openai provider，并清理跨 provider 不兼容的"
            " Responses item。默认只预览，不修改 config.toml。"
        )
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--thread-id",
        action="append",
        help="指定任务 ID；可重复传入以批量迁移",
    )
    selection.add_argument("--latest", type=int, metavar="N", help="迁移最近 N 个任务")
    selection.add_argument("--all", action="store_true", help="迁移所有用户任务")
    selection.add_argument(
        "--list",
        nargs="?",
        type=int,
        const=20,
        metavar="N",
        help="只列出最近 N 个任务及迁移状态，默认 20 个",
    )
    parser.add_argument(
        "--from-provider",
        action="append",
        default=None,
        help="只迁移指定来源 provider；可重复传入。默认迁移所有非 openai provider",
    )
    parser.add_argument(
        "--include-internal",
        action="store_true",
        help="在 --latest/--all 中包含 subagent、guardian 等内部任务",
    )
    parser.add_argument(
        "--force-drop-opaque",
        action="store_true",
        help=(
            "删除选中任务中的全部 reasoning/compaction；仅用于此前已被其他脚本"
            "提前改成 openai、原 provider 信息已丢失的任务"
        ),
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=default_codex_home(),
        help="Codex 数据目录，默认 CODEX_HOME 或 ~/.codex",
    )
    parser.add_argument("--state-db", type=Path, help="显式指定 state_*.sqlite")
    parser.add_argument("--backup-dir", type=Path, help="备份根目录")
    parser.add_argument("--apply", action="store_true", help="执行写入；默认仅预览")
    return parser.parse_args(argv)


def resolve_sqlite_home(codex_home: Path) -> Path:
    config_path = codex_home / "config.toml"
    if config_path.exists():
        try:
            # Only read the top-level sqlite_home key. Avoid a tomllib dependency
            # so the tool remains compatible with macOS system Python 3.9.
            in_table = False
            for line in config_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith("["):
                    in_table = True
                    continue
                if in_table:
                    continue
                match = re.match(
                    r'^sqlite_home\s*=\s*("(?:[^"\\]|\\.)*"|\'[^\']*\')\s*(?:#.*)?$',
                    stripped,
                )
                if match:
                    token = match.group(1)
                    raw = json.loads(token) if token.startswith('"') else token[1:-1]
                    if raw.strip():
                        return Path(raw).expanduser().resolve()
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    raw_env = os.environ.get("CODEX_SQLITE_HOME")
    if raw_env and raw_env.strip():
        path = Path(raw_env).expanduser()
        return path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()
    return codex_home


def resolve_state_db(codex_home: Path, explicit: Path | None) -> Path:
    if explicit:
        path = explicit.expanduser().resolve()
        if not path.exists():
            raise RuntimeError(f"state db 不存在: {path}")
        return path
    root = resolve_sqlite_home(codex_home)
    candidates: list[tuple[int, int, Path]] = []
    for path in root.glob("state_*.sqlite"):
        match = re.fullmatch(r"state_(\d+)\.sqlite", path.name)
        if match:
            candidates.append((int(match.group(1)), path.stat().st_mtime_ns, path))
    if not candidates:
        raise RuntimeError(f"未在 {root} 找到 state_*.sqlite")
    return max(candidates, key=lambda value: (value[0], value[1]))[2]


def is_internal_thread(info: ThreadInfo) -> bool:
    # Current Codex builds may leave has_user_event=0 even for ordinary desktop
    # tasks. The source field reliably marks spawned/guardian work as subagent.
    return '"subagent"' in info.source


def load_threads(db_path: Path) -> list[ThreadInfo]:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"SQLite integrity_check 失败: {db_path}")
        rows = connection.execute(
            """
            SELECT id, title, name, preview, model_provider, rollout_path, updated_at,
                   updated_at_ms, recency_at, recency_at_ms, archived,
                   source, has_user_event
            FROM threads
            """
        ).fetchall()
    finally:
        connection.close()

    result: list[ThreadInfo] = []
    for row in rows:
        raw_title = str(
            row["name"] or row["title"] or row["preview"] or "(无标题)"
        )
        display_title = re.sub(r"\s+", " ", raw_title).strip()
        if len(display_title) > 120:
            display_title = display_title[:119] + "…"
        timestamps = [
            int(row["updated_at"] or 0) * 1000,
            int(row["updated_at_ms"] or 0),
            int(row["recency_at"] or 0) * 1000,
            int(row["recency_at_ms"] or 0),
        ]
        result.append(
            ThreadInfo(
                thread_id=str(row["id"]),
                title=display_title,
                provider=str(row["model_provider"] or ""),
                rollout_path=Path(str(row["rollout_path"])),
                updated_at_ms=max(timestamps),
                archived=bool(row["archived"]),
                source=str(row["source"] or ""),
                has_user_event=bool(row["has_user_event"]),
            )
        )
    return result


def select_threads(args: argparse.Namespace, all_threads: list[ThreadInfo]) -> list[ThreadInfo]:
    by_id = {item.thread_id: item for item in all_threads}
    if args.thread_id:
        missing = [thread_id for thread_id in args.thread_id if thread_id not in by_id]
        if missing:
            raise RuntimeError("找不到任务 ID: " + ", ".join(missing))
        # Preserve CLI order while removing duplicates.
        return [by_id[key] for key in dict.fromkeys(args.thread_id)]

    candidates = [
        item
        for item in all_threads
        if args.include_internal or not is_internal_thread(item)
    ]
    candidates.sort(key=lambda item: item.updated_at_ms, reverse=True)
    count = args.list if args.list is not None else args.latest
    if count is not None:
        if count <= 0:
            raise RuntimeError("N 必须大于 0")
        return candidates[:count]
    return candidates


def rollout_root_id(path: Path) -> str | None:
    matches = UUID_PATTERN.findall(path.name)
    return matches[0].lower() if matches else None


def map_rollout_files(codex_home: Path, selected_ids: set[str]) -> dict[str, list[Path]]:
    result = {thread_id: [] for thread_id in selected_ids}
    for directory in ("sessions", "archived_sessions"):
        root = codex_home / directory
        if not root.exists():
            continue
        for path in root.rglob("*.jsonl"):
            root_id = rollout_root_id(path)
            if root_id in result:
                result[root_id].append(path)
    for paths in result.values():
        paths.sort()
    return result


def should_migrate_provider(
    provider: object, from_providers: set[str] | None
) -> bool:
    if not isinstance(provider, str) or not provider or provider == TARGET_PROVIDER:
        return False
    return from_providers is None or provider in from_providers


def split_line_ending(raw: bytes) -> tuple[bytes, bytes]:
    if raw.endswith(b"\r\n"):
        return raw[:-2], b"\r\n"
    if raw.endswith(b"\n"):
        return raw[:-1], b"\n"
    return raw, b""


def plan_rollout_file(
    path: Path,
    from_providers: set[str] | None,
    force_drop_opaque: bool = False,
) -> FilePlan:
    original = path.read_bytes()
    raw_lines = original.splitlines(keepends=True)
    parsed: list[dict | None] = []
    errors: list[str] = []
    providers_before: set[str] = set()

    for line_number, raw in enumerate(raw_lines, start=1):
        body, _ = split_line_ending(raw)
        if not body.strip():
            parsed.append(None)
            continue
        try:
            obj = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            errors.append(f"{path}:{line_number}: {exc}")
            parsed.append(None)
            continue
        parsed.append(obj if isinstance(obj, dict) else None)
        if isinstance(obj, dict) and obj.get("type") == "session_meta":
            payload = obj.get("payload")
            if isinstance(payload, dict):
                provider = payload.get("model_provider")
                if isinstance(provider, str) and provider:
                    providers_before.add(provider)

    cross_provider = any(
        should_migrate_provider(provider, from_providers)
        for provider in providers_before
    )
    rewritten_lines: list[bytes] = []
    meta_rewrites = 0
    removed_opaque = 0
    cleared_ids = 0
    providers_after = set(providers_before)

    for raw, obj in zip(raw_lines, parsed):
        if obj is None:
            rewritten_lines.append(raw)
            continue
        changed = False
        if obj.get("type") == "session_meta":
            payload = obj.get("payload")
            if isinstance(payload, dict):
                provider = payload.get("model_provider")
                if should_migrate_provider(provider, from_providers):
                    payload["model_provider"] = TARGET_PROVIDER
                    providers_after.discard(str(provider))
                    providers_after.add(TARGET_PROVIDER)
                    meta_rewrites += 1
                    changed = True

        if obj.get("type") == "response_item":
            payload = obj.get("payload")
            if isinstance(payload, dict):
                item_type = payload.get("type")
                item_id = payload.get("id")
                expected_opaque = OPAQUE_ID_PREFIXES.get(item_type)
                opaque_id_invalid = (
                    isinstance(item_id, str)
                    and expected_opaque is not None
                    and not item_id.startswith(expected_opaque)
                )
                if expected_opaque and (
                    force_drop_opaque or cross_provider or opaque_id_invalid
                ):
                    removed_opaque += 1
                    continue

                expected_portable = PORTABLE_ID_PREFIXES.get(item_type)
                if (
                    expected_portable
                    and isinstance(item_id, str)
                    and not item_id.startswith(expected_portable)
                ):
                    payload.pop("id", None)
                    cleared_ids += 1
                    changed = True

        if changed:
            _, ending = split_line_ending(raw)
            encoded = json.dumps(
                obj, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            rewritten_lines.append(encoded + ending)
        else:
            rewritten_lines.append(raw)

    return FilePlan(
        path=path,
        original=original,
        rewritten=b"".join(rewritten_lines),
        providers_before=providers_before,
        providers_after=providers_after,
        session_meta_rewrites=meta_rewrites,
        opaque_items_removed=removed_opaque,
        portable_ids_cleared=cleared_ids,
        parse_errors=errors,
    )


def build_plans(
    selected: list[ThreadInfo],
    rollout_map: dict[str, list[Path]],
    from_providers: set[str] | None,
    force_drop_opaque: bool = False,
) -> list[ThreadPlan]:
    plans: list[ThreadPlan] = []
    for info in selected:
        paths = list(rollout_map.get(info.thread_id, []))
        if info.rollout_path.exists() and info.rollout_path not in paths:
            paths.append(info.rollout_path)
            paths.sort()
        file_plans = [
            plan_rollout_file(path, from_providers, force_drop_opaque)
            for path in paths
        ]
        db_after = (
            TARGET_PROVIDER
            if should_migrate_provider(info.provider, from_providers)
            else info.provider
        )
        plans.append(ThreadPlan(info=info, files=file_plans, db_provider_after=db_after))
    return plans


def status_label(plan: ThreadPlan) -> str:
    if plan.parse_errors:
        return "解析错误"
    if plan.complete:
        return "已完成/无需处理"
    provider = plan.provider_changes > 0
    items = plan.opaque_items_removed + plan.portable_ids_cleared > 0
    if provider and items:
        return "需要迁移 provider + item"
    if provider:
        return "仅需迁移 provider"
    return "仅需清理 item"


def print_plans(
    plans: list[ThreadPlan], db_path: Path, codex_home: Path, apply: bool
) -> None:
    print(f"模式: {'执行写入' if apply else '预览（只读）'}")
    print(f"Codex Home: {codex_home}")
    print(f"State DB: {db_path}")
    print("目标 provider: openai")
    print("config.toml: 只读检查，不会修改")
    print()
    for index, plan in enumerate(plans, start=1):
        info = plan.info
        print(f"[{index}] {info.title}")
        print(f"    ID: {info.thread_id}")
        print(
            f"    provider: {info.provider or '(空)'} -> {plan.db_provider_after or '(空)'}"
        )
        print(
            f"    状态: {status_label(plan)}; rollout={len(plan.files)}, "
            f"provider_changes={plan.provider_changes}, "
            f"remove_opaque={plan.opaque_items_removed}, "
            f"clear_ids={plan.portable_ids_cleared}"
        )
        for error in plan.parse_errors[:3]:
            print(f"    错误: {error}")
        if len(plan.parse_errors) > 3:
            print(f"    另有 {len(plan.parse_errors) - 3} 个解析错误")
    print()
    print(
        "汇总: "
        f"tasks={len(plans)}, "
        f"complete={sum(plan.complete for plan in plans)}, "
        f"changed_files={sum(len(plan.changed_files) for plan in plans)}, "
        f"db_updates={sum(plan.db_needs_update for plan in plans)}, "
        f"remove_opaque={sum(plan.opaque_items_removed for plan in plans)}, "
        f"clear_ids={sum(plan.portable_ids_cleared for plan in plans)}"
    )
    if not apply:
        print("当前为预览模式；确认结果后追加 --apply 才会写入。")


def atomic_write(path: Path, content: bytes) -> None:
    mode = path.stat().st_mode
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)
        raise


def sqlite_snapshot(source: Path, destination: Path) -> None:
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        result = destination_connection.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"SQLite 备份完整性检查失败: {result}")
    finally:
        destination_connection.close()
        source_connection.close()


@contextlib.contextmanager
def migration_locks(codex_home: Path, thread_ids: list[str]) -> Iterator[None]:
    lock_specs: list[tuple[Path, bool]] = []
    maintenance = codex_home / ".tmp" / "rollout-maintenance.lock"
    lock_specs.append((maintenance, maintenance.exists()))
    lock_dir = codex_home / "thread-writer-locks"
    for thread_id in thread_ids:
        path = lock_dir / f"{thread_id}.lock"
        lock_specs.append((path, path.exists()))

    handles: list[tuple[object, Path, bool]] = []
    try:
        for path, existed in lock_specs:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+b")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                handle.close()
                if path.parent == lock_dir:
                    raise RuntimeError(
                        f"任务仍被 Codex 打开，无法安全迁移: {path.stem}。"
                        "请关闭/归档该任务或退出 Codex 后重试。"
                    ) from exc
                raise RuntimeError("已有另一个历史迁移程序正在运行") from exc
            handles.append((handle, path, existed))
        yield
    finally:
        for handle, path, existed in reversed(handles):
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            if not existed:
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()


def make_backup(
    plans: list[ThreadPlan],
    db_path: Path,
    codex_home: Path,
    backup_root: Path,
) -> Path:
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = backup_root / timestamp
    suffix = 1
    while destination.exists():
        destination = backup_root / f"{timestamp}-{suffix}"
        suffix += 1
    destination.mkdir(parents=True)

    manifest: dict[str, object] = {
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "target_provider": TARGET_PROVIDER,
        "state_db": str(db_path),
        "tasks": [],
    }
    task_entries: list[dict[str, object]] = []
    for plan in plans:
        files: list[dict[str, object]] = []
        for file_plan in plan.changed_files:
            try:
                relative = file_plan.path.relative_to(codex_home)
            except ValueError:
                relative = Path("external") / file_plan.path.name
            backup_path = destination / "rollouts" / relative
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path.write_bytes(file_plan.original)
            files.append(
                {
                    "path": str(file_plan.path),
                    "backup": str(backup_path),
                    "sha256_before": hashlib.sha256(file_plan.original).hexdigest(),
                    "sha256_after": hashlib.sha256(file_plan.rewritten).hexdigest(),
                    "session_meta_rewrites": file_plan.session_meta_rewrites,
                    "opaque_items_removed": file_plan.opaque_items_removed,
                    "portable_ids_cleared": file_plan.portable_ids_cleared,
                }
            )
        task_entries.append(
            {
                "id": plan.info.thread_id,
                "title": plan.info.title,
                "db_provider_before": plan.info.provider,
                "db_provider_after": plan.db_provider_after,
                "files": files,
            }
        )
    manifest["tasks"] = task_entries
    if any(plan.db_needs_update for plan in plans):
        sqlite_snapshot(db_path, destination / "state.sqlite.snapshot")
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination


def apply_plans(
    plans: list[ThreadPlan],
    db_path: Path,
    codex_home: Path,
    backup_root: Path,
    from_providers: set[str] | None,
    force_drop_opaque: bool = False,
) -> Path | None:
    errors = [error for plan in plans for error in plan.parse_errors]
    if errors:
        raise RuntimeError(
            "选中任务包含无法解析的 JSONL 行；为避免损坏，未执行任何写入。\n"
            + "\n".join(errors[:20])
        )
    if all(plan.complete for plan in plans):
        return None

    pending_plans = [plan for plan in plans if not plan.complete]
    with migration_locks(
        codex_home, [plan.info.thread_id for plan in pending_plans]
    ):
        # Re-read after acquiring locks to close the preview/apply race window.
        thread_map = {item.thread_id: item for item in load_threads(db_path)}
        refreshed_infos = [thread_map[plan.info.thread_id] for plan in pending_plans]
        rollout_map = map_rollout_files(
            codex_home, {item.thread_id for item in refreshed_infos}
        )
        refreshed = build_plans(
            refreshed_infos,
            rollout_map,
            from_providers,
            force_drop_opaque,
        )
        errors = [error for plan in refreshed for error in plan.parse_errors]
        if errors:
            raise RuntimeError("加锁后复检发现 JSONL 解析错误，未写入")
        if all(plan.complete for plan in refreshed):
            return None

        backup_dir = make_backup(refreshed, db_path, codex_home, backup_root)
        written: list[FilePlan] = []
        connection = sqlite3.connect(db_path, timeout=30)
        try:
            for plan in refreshed:
                for file_plan in plan.changed_files:
                    atomic_write(file_plan.path, file_plan.rewritten)
                    written.append(file_plan)

            connection.execute("BEGIN IMMEDIATE")
            for plan in refreshed:
                if not plan.db_needs_update:
                    continue
                cursor = connection.execute(
                    """
                    UPDATE threads SET model_provider = ?
                    WHERE id = ? AND model_provider = ?
                    """,
                    (
                        plan.db_provider_after,
                        plan.info.thread_id,
                        plan.info.provider,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        f"SQLite provider 更新数量异常: {plan.info.thread_id}"
                    )
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite 写入后 integrity_check 失败")
            connection.commit()
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                connection.rollback()
            for file_plan in reversed(written):
                atomic_write(file_plan.path, file_plan.original)
            raise
        finally:
            connection.close()
        return backup_dir


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list is not None and args.apply:
        raise RuntimeError("--list 不能与 --apply 同时使用")
    codex_home = args.codex_home.expanduser().resolve()
    db_path = resolve_state_db(codex_home, args.state_db)
    all_threads = load_threads(db_path)
    selected = select_threads(args, all_threads)
    if not selected:
        raise RuntimeError("没有符合条件的任务")
    selected_ids = {item.thread_id for item in selected}
    rollout_map = map_rollout_files(codex_home, selected_ids)
    from_providers = set(args.from_provider) if args.from_provider else None
    plans = build_plans(
        selected, rollout_map, from_providers, args.force_drop_opaque
    )
    print_plans(plans, db_path, codex_home, args.apply)

    if not args.apply:
        return 0
    backup_root = (
        args.backup_dir.expanduser().resolve()
        if args.backup_dir
        else Path(__file__).resolve().parent / "backups" / "history"
    )
    backup_dir = apply_plans(
        plans,
        db_path,
        codex_home,
        backup_root,
        from_providers,
        args.force_drop_opaque,
    )
    if backup_dir is None:
        print("没有需要写入的内容；迁移已完成。")
    else:
        print(f"迁移完成。备份目录: {backup_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, sqlite3.Error) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(2)
