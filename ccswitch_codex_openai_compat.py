#!/usr/bin/env python3
"""Make CC Switch Codex presets share the built-in ``openai`` history bucket.

Besides normalizing stored presets, ``--activate`` writes a compatible live
Codex config and selects the same card in CC Switch. This explicit activation
path is required by CC Switch 3.20.2, whose normal GUI switch intentionally
rewrites keyed ``openai_base_url`` presets into a custom ``cc-switch`` provider.

By default the script is read-only. Every write creates a consistent SQLite
snapshot, and live activation also backs up the exact Codex config/auth files.

Compatible with the macOS system Python 3.9; no third-party dependencies.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


OFFICIAL_PROVIDER_ID = "codex-official"
TARGET_PROVIDER = "openai"
DEFAULT_DB = Path.home() / ".cc-switch" / "cc-switch.db"
DEFAULT_CODEX_HOME = Path.home() / ".codex"
DEFAULT_BACKUP_ROOT = Path(__file__).resolve().parent / "backups" / "ccswitch"
RESPONSES_API_FORMAT = "openai_responses"
MODEL_CATALOG_FILENAME = "cc-switch-model-catalog.json"
DEFAULT_CONTEXT_WINDOW = 262_144

SUPPORTED_CUSTOM_KEYS = {
    "name",
    "base_url",
    "wire_api",
    "requires_openai_auth",
}

TABLE_HEADER_RE = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")
ASSIGNMENT_RE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")


class ConversionError(RuntimeError):
    """Raised when a preset cannot be converted without losing behavior."""


@dataclass(frozen=True)
class ProviderRecord:
    provider_id: str
    name: str
    is_current: bool
    settings_config_raw: str
    settings: Dict[str, object]
    config_text: str
    api_format: Optional[str]


@dataclass(frozen=True)
class ConfigAnalysis:
    active_provider: Optional[str]
    openai_base_url: Optional[str]
    provider_tables: Dict[str, Dict[str, object]]


@dataclass(frozen=True)
class ConversionPlan:
    record: ProviderRecord
    before: ConfigAnalysis
    rewritten_config: str
    rewritten_settings_raw: str
    changed: bool
    catalog_text: Optional[str] = None
    error: Optional[str] = None

    @property
    def status(self) -> str:
        if self.error:
            return "不可转换"
        if self.changed:
            return "可转换"
        return "已兼容"


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "将 CC Switch 的第三方 Codex 配置改为内建 openai provider + "
            "openai_base_url，使其与官方任务共用历史 bucket。"
        )
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--list",
        action="store_true",
        help="列出全部 Codex 配置及兼容状态（只读）",
    )
    selection.add_argument(
        "--provider",
        action="append",
        metavar="NAME_OR_ID",
        help="按精确名称或 ID 选择配置；可重复传入",
    )
    selection.add_argument(
        "--all-third-party",
        action="store_true",
        help="选择除 OpenAI Official 外的全部 Codex 配置",
    )
    selection.add_argument(
        "--activate",
        metavar="NAME_OR_ID",
        help=(
            "兼容方式激活一个配置；支持第三方和 OpenAI Official。"
            "不带 --apply 时仅预览"
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help="CC Switch SQLite 数据库路径",
    )
    parser.add_argument(
        "--backup-root",
        type=Path,
        default=DEFAULT_BACKUP_ROOT,
        help="自动备份根目录",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=DEFAULT_CODEX_HOME,
        help="Codex 配置目录（默认 ~/.codex）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="执行写入；默认仅预览",
    )
    return parser.parse_args(argv)


def decode_toml_string(token: str) -> str:
    token = token.strip()
    if len(token) < 2:
        raise ConversionError("字符串值格式无效")
    if token[0] == '"' and token[-1] == '"':
        try:
            value = json.loads(token)
        except json.JSONDecodeError as exc:
            raise ConversionError(f"无法解析 TOML 字符串: {token}") from exc
        if not isinstance(value, str):
            raise ConversionError("TOML 值不是字符串")
        return value
    if token[0] == "'" and token[-1] == "'":
        return token[1:-1]
    raise ConversionError(f"只支持普通 TOML 字符串: {token}")


def encode_toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def split_toml_path(raw: str) -> Tuple[str, ...]:
    """Parse the small TOML dotted-key subset used by table headers."""

    parts: List[str] = []
    index = 0
    length = len(raw)
    while index < length:
        while index < length and raw[index].isspace():
            index += 1
        if index >= length:
            break
        if raw[index] in ('"', "'"):
            quote = raw[index]
            start = index
            index += 1
            escaped = False
            while index < length:
                char = raw[index]
                if quote == '"' and char == "\\" and not escaped:
                    escaped = True
                    index += 1
                    continue
                if char == quote and not escaped:
                    index += 1
                    break
                escaped = False
                index += 1
            else:
                raise ConversionError(f"无法解析表头: [{raw}]")
            parts.append(decode_toml_string(raw[start:index]))
        else:
            match = re.match(r"[A-Za-z0-9_-]+", raw[index:])
            if not match:
                raise ConversionError(f"无法解析表头: [{raw}]")
            parts.append(match.group(0))
            index += len(match.group(0))
        while index < length and raw[index].isspace():
            index += 1
        if index >= length:
            break
        if raw[index] != ".":
            raise ConversionError(f"无法解析表头: [{raw}]")
        index += 1
    if not parts:
        raise ConversionError(f"空表头: [{raw}]")
    return tuple(parts)


def table_path(line: str) -> Optional[Tuple[str, ...]]:
    match = TABLE_HEADER_RE.match(line.rstrip("\r\n"))
    if not match:
        return None
    return split_toml_path(match.group(1))


def parse_string_assignment(line: str, key: str) -> Optional[str]:
    pattern = re.compile(
        r"^\s*"
        + re.escape(key)
        + r"\s*=\s*(\"(?:\\.|[^\"\\])*\"|'[^']*')\s*(?:#.*)?$"
    )
    match = pattern.match(line.rstrip("\r\n"))
    return decode_toml_string(match.group(1)) if match else None


def parse_bool_assignment(line: str, key: str) -> Optional[bool]:
    match = re.match(
        r"^\s*" + re.escape(key) + r"\s*=\s*(true|false)\s*(?:#.*)?$",
        line.rstrip("\r\n"),
    )
    if not match:
        return None
    return match.group(1) == "true"


def analyze_config(config_text: str) -> ConfigAnalysis:
    current: Tuple[str, ...] = ()
    active_provider: Optional[str] = None
    openai_base_url: Optional[str] = None
    providers: Dict[str, Dict[str, object]] = {}

    for line in config_text.splitlines(keepends=True):
        header = table_path(line)
        if header is not None:
            current = header
            if len(current) >= 2 and current[0] == "model_providers":
                providers.setdefault(current[1], {})
                if len(current) > 2:
                    providers[current[1]].setdefault("__nested_tables__", []).append(
                        current[2:]
                    )
            continue

        if not current:
            value = parse_string_assignment(line, "model_provider")
            if value is not None:
                active_provider = value
                continue
            value = parse_string_assignment(line, "openai_base_url")
            if value is not None:
                openai_base_url = value
                continue

        if len(current) == 2 and current[0] == "model_providers":
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            assignment = ASSIGNMENT_RE.match(line)
            if not assignment:
                providers[current[1]].setdefault("__unparsed__", []).append(stripped)
                continue
            key = assignment.group(1)
            string_value = parse_string_assignment(line, key)
            if string_value is not None:
                providers[current[1]][key] = string_value
                continue
            bool_value = parse_bool_assignment(line, key)
            if bool_value is not None:
                providers[current[1]][key] = bool_value
                continue
            providers[current[1]].setdefault("__unparsed__", []).append(stripped)

    return ConfigAnalysis(
        active_provider=active_provider,
        openai_base_url=openai_base_url,
        provider_tables=providers,
    )


def replace_or_insert_top_level_string(
    lines: List[str], key: str, value: str, after_key: Optional[str] = None
) -> List[str]:
    current: Tuple[str, ...] = ()
    result = list(lines)
    for index, line in enumerate(result):
        header = table_path(line)
        if header is not None:
            current = header
            continue
        if current:
            continue
        if parse_string_assignment(line, key) is not None:
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            result[index] = f"{key} = {encode_toml_string(value)}{ending}"
            return result

    first_table = next(
        (index for index, line in enumerate(result) if table_path(line) is not None),
        len(result),
    )
    insert_at = first_table
    if after_key:
        current = ()
        for index, line in enumerate(result[:first_table]):
            if table_path(line) is not None:
                break
            if parse_string_assignment(line, after_key) is not None:
                insert_at = index + 1
                break
    result.insert(insert_at, f"{key} = {encode_toml_string(value)}\n")
    return result


def remove_top_level_assignment(lines: Iterable[str], key: str) -> List[str]:
    current: Tuple[str, ...] = ()
    result: List[str] = []
    key_pattern = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
    for line in lines:
        header = table_path(line)
        if header is not None:
            current = header
        if not current and key_pattern.match(line):
            continue
        result.append(line)
    return result


def configure_model_catalog_reference(
    config_text: str, catalog_text: Optional[str]
) -> str:
    lines = remove_top_level_assignment(
        config_text.splitlines(keepends=True), "model_catalog_json"
    )
    if catalog_text is not None:
        lines = replace_or_insert_top_level_string(
            lines, "model_catalog_json", MODEL_CATALOG_FILENAME, after_key="model"
        )
    return "".join(lines)


def build_codex_model_catalog(settings: Dict[str, object]) -> Optional[str]:
    """Project CC Switch's compact modelCatalog into a Codex catalog file.

    The generated entries use a conservative native-Responses capability
    profile. They list models without claiming unsupported Codex-specific
    freeform tools or search endpoints.
    """

    compact = settings.get("modelCatalog")
    if compact is None:
        return None
    if not isinstance(compact, dict):
        raise ConversionError("modelCatalog 不是对象")
    models = compact.get("models")
    if not isinstance(models, list):
        raise ConversionError("modelCatalog.models 不是数组")
    if not models:
        return None

    entries: List[Dict[str, object]] = []
    seen = set()
    for index, item in enumerate(models):
        if not isinstance(item, dict):
            raise ConversionError(f"modelCatalog.models[{index}] 不是对象")
        model = item.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ConversionError(f"modelCatalog.models[{index}] 缺少 model")
        model = model.strip()
        if model in seen:
            raise ConversionError(f"modelCatalog 存在重复模型: {model}")
        seen.add(model)

        display_name = item.get("displayName", model)
        if not isinstance(display_name, str) or not display_name.strip():
            display_name = model
        context_window = item.get("contextWindow", DEFAULT_CONTEXT_WINDOW)
        if (
            isinstance(context_window, bool)
            or not isinstance(context_window, int)
            or context_window <= 0
        ):
            raise ConversionError(f"模型 {model} 的 contextWindow 无效")

        input_modalities = item.get("inputModalities", ["text", "image"])
        if (
            not isinstance(input_modalities, list)
            or not input_modalities
            or any(value not in {"text", "image"} for value in input_modalities)
        ):
            raise ConversionError(f"模型 {model} 的 inputModalities 无效")
        parallel = item.get("supportsParallelToolCalls", False)
        if not isinstance(parallel, bool):
            raise ConversionError(f"模型 {model} 的 supportsParallelToolCalls 无效")
        base_instructions = item.get(
            "baseInstructions",
            "You are Codex, a coding agent. You and the user share the same "
            "workspace and collaborate to achieve the user's goals.",
        )
        if not isinstance(base_instructions, str) or not base_instructions.strip():
            raise ConversionError(f"模型 {model} 的 baseInstructions 无效")

        entries.append(
            {
                "slug": model,
                "display_name": display_name.strip(),
                "description": display_name.strip(),
                "base_instructions": base_instructions,
                "default_reasoning_level": "high",
                "supported_reasoning_levels": [
                    {"effort": "none", "description": "Disable Thinking"},
                    {"effort": "high", "description": "Enabled Thinking"},
                ],
                "shell_type": "shell_command",
                "visibility": "list",
                "supported_in_api": True,
                "priority": index,
                "supports_reasoning_summaries": True,
                "default_reasoning_summary": "none",
                "support_verbosity": False,
                "truncation_policy": {"mode": "bytes", "limit": 10_000},
                "supports_parallel_tool_calls": parallel,
                "supports_image_detail_original": False,
                "context_window": context_window,
                "max_context_window": context_window,
                "effective_context_window_percent": 95,
                "experimental_supported_tools": [],
                "input_modalities": input_modalities,
                "supports_search_tool": False,
            }
        )
    return json.dumps({"models": entries}, ensure_ascii=False, indent=2) + "\n"


def validate_upstream_api_format(record: ProviderRecord) -> None:
    """Use CC Switch's upstream selector, not Codex's always-Responses wire_api."""

    if record.api_format == RESPONSES_API_FORMAT:
        return
    if record.api_format is None:
        raise ConversionError(
            "CC Switch meta.apiFormat 缺失；无法确认上游是否原生支持 Responses API"
        )
    labels = {
        "openai_chat": "OpenAI Chat Completions",
        "anthropic": "Anthropic Messages",
    }
    label = labels.get(record.api_format, record.api_format)
    raise ConversionError(
        f"上游格式为 {label} ({record.api_format})，需要 CC Switch 本地路由做协议转换；"
        "不能使用 openai_base_url 直连兼容激活"
    )


def remove_provider_table(lines: Iterable[str], provider_id: str) -> List[str]:
    result: List[str] = []
    skipping = False
    target_prefix = ("model_providers", provider_id)
    for line in lines:
        header = table_path(line)
        if header is not None:
            skipping = header[:2] == target_prefix
        if not skipping:
            result.append(line)
    return result


def convert_config(config_text: str) -> Tuple[str, ConfigAnalysis]:
    before = analyze_config(config_text)
    active = before.active_provider
    if active == TARGET_PROVIDER:
        if not before.openai_base_url:
            raise ConversionError(
                "第三方配置已使用 openai，但缺少 openai_base_url；无法确认目标后端"
            )
        return config_text, before
    if not active:
        raise ConversionError("缺少顶层 model_provider")
    if active in {"ollama", "lmstudio"}:
        raise ConversionError(f"不应把内建本地 provider {active} 转成 openai")

    provider = before.provider_tables.get(active)
    if provider is None:
        raise ConversionError(f"找不到 [model_providers.{active}] 定义")
    if provider.get("__nested_tables__"):
        raise ConversionError("provider 含嵌套认证表，无法仅用 openai_base_url 无损转换")
    if provider.get("__unparsed__"):
        raise ConversionError(
            "provider 含无法安全解析的字段: "
            + "; ".join(str(value) for value in provider["__unparsed__"])
        )
    unsupported = sorted(set(provider) - SUPPORTED_CUSTOM_KEYS)
    if unsupported:
        raise ConversionError("provider 含无法映射的字段: " + ", ".join(unsupported))
    base_url = provider.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        raise ConversionError("自定义 provider 缺少 base_url")
    wire_api = provider.get("wire_api")
    if wire_api not in (None, "responses"):
        raise ConversionError(f"仅支持 Responses API，当前 wire_api={wire_api!r}")

    lines = remove_provider_table(config_text.splitlines(keepends=True), active)
    lines = replace_or_insert_top_level_string(lines, "model_provider", TARGET_PROVIDER)
    lines = replace_or_insert_top_level_string(
        lines, "openai_base_url", base_url.strip(), after_key="model_provider"
    )
    rewritten = "".join(lines)
    after = analyze_config(rewritten)
    if after.active_provider != TARGET_PROVIDER:
        raise ConversionError("转换后 model_provider 校验失败")
    if after.openai_base_url != base_url.strip():
        raise ConversionError("转换后 openai_base_url 校验失败")
    if active in after.provider_tables:
        raise ConversionError("转换后仍残留旧 provider 定义")
    return rewritten, before


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def verify_database(connection: sqlite3.Connection) -> None:
    result = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise RuntimeError(f"SQLite integrity_check 失败: {result}")
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(providers)").fetchall()
    }
    required = {"id", "app_type", "name", "settings_config", "meta", "is_current"}
    missing = required - columns
    if missing:
        raise RuntimeError("providers 表缺少字段: " + ", ".join(sorted(missing)))


def load_records(db_path: Path) -> List[ProviderRecord]:
    if not db_path.exists():
        raise RuntimeError(f"CC Switch 数据库不存在: {db_path}")
    connection = connect_readonly(db_path)
    try:
        verify_database(connection)
        rows = connection.execute(
            """
            SELECT id, name, is_current, settings_config, meta
            FROM providers
            WHERE app_type = 'codex'
            ORDER BY is_current DESC, sort_index, name
            """
        ).fetchall()
    finally:
        connection.close()

    records: List[ProviderRecord] = []
    for row in rows:
        raw = str(row["settings_config"])
        try:
            settings = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"配置 {row['name']} 的 settings_config 不是有效 JSON") from exc
        if not isinstance(settings, dict):
            raise RuntimeError(f"配置 {row['name']} 的 settings_config 不是对象")
        try:
            meta = json.loads(str(row["meta"] or "{}"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"配置 {row['name']} 的 meta 不是有效 JSON") from exc
        if not isinstance(meta, dict):
            raise RuntimeError(f"配置 {row['name']} 的 meta 不是对象")
        api_format = meta.get("apiFormat")
        if api_format is not None and not isinstance(api_format, str):
            raise RuntimeError(f"配置 {row['name']} 的 meta.apiFormat 不是字符串")
        config_text = settings.get("config")
        if not isinstance(config_text, str):
            raise RuntimeError(f"配置 {row['name']} 缺少文本 config")
        records.append(
            ProviderRecord(
                provider_id=str(row["id"]),
                name=str(row["name"]),
                is_current=bool(row["is_current"]),
                settings_config_raw=raw,
                settings=settings,
                config_text=config_text,
                api_format=api_format,
            )
        )
    return records


def is_official(record: ProviderRecord) -> bool:
    return record.provider_id == OFFICIAL_PROVIDER_ID


def select_records(
    records: Sequence[ProviderRecord], identifiers: Optional[Sequence[str]], all_third_party: bool
) -> List[ProviderRecord]:
    if all_third_party:
        return [record for record in records if not is_official(record)]
    if not identifiers:
        return list(records)

    selected: List[ProviderRecord] = []
    for identifier in identifiers:
        matches = [
            record
            for record in records
            if record.provider_id == identifier or record.name == identifier
        ]
        if not matches:
            raise RuntimeError(f"找不到 Codex 配置: {identifier}")
        if len(matches) > 1:
            raise RuntimeError(f"配置名称/ID 不唯一: {identifier}，请改用精确 ID")
        if is_official(matches[0]):
            raise RuntimeError("拒绝修改 OpenAI Official 配置")
        if matches[0] not in selected:
            selected.append(matches[0])
    return selected


def build_plan(record: ProviderRecord) -> ConversionPlan:
    before = analyze_config(record.config_text)
    if is_official(record):
        return ConversionPlan(
            record=record,
            before=before,
            rewritten_config=record.config_text,
            rewritten_settings_raw=record.settings_config_raw,
            changed=False,
            catalog_text=None,
            error=None,
        )
    try:
        validate_upstream_api_format(record)
        rewritten_config, before = convert_config(record.config_text)
        settings = dict(record.settings)
        catalog_text = build_codex_model_catalog(settings)
        rewritten_config = configure_model_catalog_reference(
            rewritten_config, catalog_text
        )
        settings["config"] = rewritten_config
        rewritten_raw = json.dumps(settings, ensure_ascii=False, separators=(",", ":"))
        return ConversionPlan(
            record=record,
            before=before,
            rewritten_config=rewritten_config,
            rewritten_settings_raw=rewritten_raw,
            changed=rewritten_config != record.config_text,
            catalog_text=catalog_text,
        )
    except ConversionError as exc:
        return ConversionPlan(
            record=record,
            before=before,
            rewritten_config=record.config_text,
            rewritten_settings_raw=record.settings_config_raw,
            changed=False,
            catalog_text=None,
            error=str(exc),
        )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def print_plans(plans: Sequence[ConversionPlan], apply: bool, db_path: Path) -> None:
    print(f"模式: {'执行写入' if apply else '预览（只读）'}")
    print(f"CC Switch DB: {db_path}")
    print()
    for plan in plans:
        marker = "*" if plan.record.is_current else " "
        print(f"{marker} {plan.record.name}  [{plan.record.provider_id}]")
        display_status = "官方配置（不会修改）" if is_official(plan.record) else plan.status
        print(f"  状态: {display_status}")
        print(f"  model_provider: {plan.before.active_provider or '(缺失)'}")
        print(f"  openai_base_url: {plan.before.openai_base_url or '(缺失)'}")
        if not is_official(plan.record):
            print(f"  上游 API 格式: {plan.record.api_format or '(缺失)'}")
            if plan.catalog_text is not None:
                model_count = len(json.loads(plan.catalog_text)["models"])
                print(f"  模型目录: {model_count} 个模型")
        if plan.changed:
            after = analyze_config(plan.rewritten_config)
            print(f"  转换后: model_provider={after.active_provider}")
            print(f"  转换后: openai_base_url={after.openai_base_url}")
        if plan.error:
            print(f"  原因: {plan.error}")
    print()
    print(
        f"汇总: selected={len(plans)}, "
        f"convertible={sum(plan.changed and not plan.error for plan in plans)}, "
        f"compatible={sum(not plan.changed and not plan.error for plan in plans)}, "
        f"blocked={sum(bool(plan.error) for plan in plans)}"
    )
    if not apply:
        print("当前为预览模式；确认后追加 --apply 才会写入。")


def sqlite_snapshot(source: Path, destination: Path) -> None:
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        verify_database(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
    os.chmod(destination, 0o600)


def create_backup(
    db_path: Path,
    backup_root: Path,
    plans: Sequence[ConversionPlan],
    live_paths: Sequence[Path] = (),
) -> Path:
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = backup_root.expanduser().resolve() / timestamp
    suffix = 1
    while destination.exists():
        destination = backup_root.expanduser().resolve() / f"{timestamp}-{suffix}"
        suffix += 1
    destination.mkdir(parents=True, mode=0o700)
    os.chmod(destination, 0o700)
    snapshot = destination / "cc-switch.db.snapshot"
    sqlite_snapshot(db_path, snapshot)
    live_manifest = []
    live_dir = destination / "codex-live"
    for source in live_paths:
        item = {"source": str(source), "existed": source.exists()}
        if source.exists():
            live_dir.mkdir(mode=0o700, exist_ok=True)
            os.chmod(live_dir, 0o700)
            target = live_dir / source.name
            shutil.copy2(source, target)
            os.chmod(target, 0o600)
            item.update(
                {
                    "backup": str(target.relative_to(destination)),
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                }
            )
        live_manifest.append(item)
    manifest = {
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_db": str(db_path),
        "snapshot": snapshot.name,
        "providers": [
            {
                "id": plan.record.provider_id,
                "name": plan.record.name,
                "settings_sha256_before": sha256_text(plan.record.settings_config_raw),
                "settings_sha256_after": sha256_text(plan.rewritten_settings_raw),
            }
            for plan in plans
            if plan.changed
        ],
        "live_files": live_manifest,
    }
    manifest_path = destination / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.chmod(manifest_path, 0o600)
    return destination


def extract_api_key(record: ProviderRecord) -> str:
    auth = record.settings.get("auth")
    if not isinstance(auth, dict):
        raise RuntimeError(f"配置 {record.name} 缺少 auth 对象")
    value = auth.get("OPENAI_API_KEY")
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"配置 {record.name} 缺少 OPENAI_API_KEY")
    return value.strip()


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def restore_live_file(path: Path, previous: Optional[bytes]) -> None:
    if previous is None:
        if path.exists():
            path.unlink()
        return
    atomic_write(path, previous)


def proxy_route_enabled(db_path: Path) -> bool:
    connection = connect_readonly(db_path)
    try:
        row = connection.execute(
            "SELECT enabled FROM proxy_config WHERE app_type = 'codex'"
        ).fetchone()
        return bool(row and row[0])
    except sqlite3.OperationalError:
        return False
    finally:
        connection.close()


def build_live_activation(
    record: ProviderRecord, plan: ConversionPlan
) -> Tuple[str, Dict[str, object], Optional[str]]:
    if is_official(record):
        auth = record.settings.get("auth")
        if not isinstance(auth, dict) or not auth:
            raise RuntimeError("OpenAI Official 没有已保存的登录信息")
        return record.config_text, dict(auth), None

    if plan.error:
        raise RuntimeError(f"配置 {record.name} 无法兼容激活: {plan.error}")
    key = extract_api_key(record)
    analysis = analyze_config(plan.rewritten_config)
    if analysis.active_provider != TARGET_PROVIDER or not analysis.openai_base_url:
        raise RuntimeError("兼容 live config 校验失败")
    # The built-in provider reads its API-key credential from auth.json. Never
    # mix a third-party key with the user's ChatGPT OAuth token bundle.
    return plan.rewritten_config, {"OPENAI_API_KEY": key}, plan.catalog_text


def activate_record(
    db_path: Path,
    backup_root: Path,
    codex_home: Path,
    record: ProviderRecord,
    plan: ConversionPlan,
) -> Path:
    if proxy_route_enabled(db_path):
        raise RuntimeError("Codex 本地路由仍处于开启状态；请先在 CC Switch 中关闭")

    config_text, live_auth, catalog_text = build_live_activation(record, plan)
    config_path = codex_home / "config.toml"
    auth_path = codex_home / "auth.json"
    catalog_path = codex_home / MODEL_CATALOG_FILENAME
    backup = create_backup(
        db_path, backup_root, (plan,), (config_path, auth_path, catalog_path)
    )

    previous_config = config_path.read_bytes() if config_path.exists() else None
    previous_auth = auth_path.read_bytes() if auth_path.exists() else None
    previous_catalog = catalog_path.read_bytes() if catalog_path.exists() else None
    atomic_write(config_path, config_text.encode("utf-8"))
    try:
        atomic_write(
            auth_path,
            (json.dumps(live_auth, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        if catalog_text is None:
            if catalog_path.exists():
                catalog_path.unlink()
        else:
            atomic_write(catalog_path, catalog_text.encode("utf-8"))
        connection = sqlite3.connect(db_path, timeout=10)
        try:
            connection.execute("PRAGMA busy_timeout=10000")
            verify_database(connection)
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT settings_config FROM providers "
                "WHERE id = ? AND app_type = 'codex'",
                (record.provider_id,),
            ).fetchone()
            if current is None or str(current[0]) != record.settings_config_raw:
                raise RuntimeError(f"配置在预览后发生变化: {record.name}")
            if plan.changed:
                connection.execute(
                    "UPDATE providers SET settings_config = ? "
                    "WHERE id = ? AND app_type = 'codex'",
                    (plan.rewritten_settings_raw, record.provider_id),
                )
            connection.execute(
                "UPDATE providers SET is_current = CASE WHEN id = ? THEN 1 ELSE 0 END "
                "WHERE app_type = 'codex'",
                (record.provider_id,),
            )
            connection.commit()
            verify_database(connection)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
    except BaseException:
        restore_live_file(config_path, previous_config)
        restore_live_file(auth_path, previous_auth)
        restore_live_file(catalog_path, previous_catalog)
        raise
    return backup


def apply_plans(
    db_path: Path, backup_root: Path, plans: Sequence[ConversionPlan]
) -> Optional[Path]:
    blocked = [plan for plan in plans if plan.error]
    if blocked:
        raise RuntimeError("存在不可转换配置；未写入任何内容")
    changed = [plan for plan in plans if plan.changed]
    if not changed:
        return None

    backup = create_backup(db_path, backup_root, changed)
    connection = sqlite3.connect(db_path, timeout=10)
    try:
        connection.execute("PRAGMA busy_timeout=10000")
        verify_database(connection)
        connection.execute("BEGIN IMMEDIATE")
        for plan in changed:
            current = connection.execute(
                """
                SELECT settings_config FROM providers
                WHERE id = ? AND app_type = 'codex'
                """,
                (plan.record.provider_id,),
            ).fetchone()
            if current is None:
                raise RuntimeError(f"配置已消失: {plan.record.name}")
            if str(current[0]) != plan.record.settings_config_raw:
                raise RuntimeError(f"配置在预览后发生变化: {plan.record.name}")
            cursor = connection.execute(
                """
                UPDATE providers SET settings_config = ?
                WHERE id = ? AND app_type = 'codex' AND settings_config = ?
                """,
                (
                    plan.rewritten_settings_raw,
                    plan.record.provider_id,
                    plan.record.settings_config_raw,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"更新失败或发生并发修改: {plan.record.name}")
        connection.commit()
        verify_database(connection)
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return backup


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    db_path = args.db.expanduser().resolve()
    try:
        records = load_records(db_path)
        if args.activate:
            matches = [
                record
                for record in records
                if record.name == args.activate or record.provider_id == args.activate
            ]
            if not matches:
                raise RuntimeError(f"找不到 Codex 配置: {args.activate}")
            if len(matches) > 1:
                raise RuntimeError(f"配置名称/ID 不唯一: {args.activate}，请改用精确 ID")
            selected = matches
        else:
            selected = select_records(records, args.provider, args.all_third_party)
        plans = [build_plan(record) for record in selected]
        print_plans(plans, args.apply, db_path)
        if args.list:
            if args.apply:
                raise RuntimeError("--list 不能与 --apply 一起使用")
            return 0
        if args.activate:
            record = selected[0]
            plan = plans[0]
            if plan.error:
                raise RuntimeError(f"配置 {record.name} 无法兼容激活: {plan.error}")
            if is_official(record):
                print("激活预览: 恢复 CC Switch 保存的 ChatGPT 登录和官方配置")
            else:
                after = analyze_config(plan.rewritten_config)
                print("激活预览: 使用内建 openai 历史 bucket")
                print(f"激活预览: openai_base_url={after.openai_base_url}")
                print(f"激活预览: 上游 API 格式={record.api_format}")
                if plan.catalog_text is not None:
                    count = len(json.loads(plan.catalog_text)["models"])
                    print(f"激活预览: 写入模型目录（{count} 个模型）")
                else:
                    print("激活预览: 无模型目录，清理旧的 CC Switch 模型目录")
                print("激活预览: 使用该卡已保存的 API key（不会显示）")
            if not args.apply:
                print("当前为预览模式；确认后追加 --apply 才会激活。")
                return 0
            backup = activate_record(
                db_path,
                args.backup_root,
                args.codex_home.expanduser().resolve(),
                record,
                plan,
            )
            print(f"备份: {backup}")
            print(f"已兼容激活: {record.name}")
            print("请重启 Codex 客户端；不要再点击 CC Switch 内该卡的“启用”。")
        elif args.apply:
            backup = apply_plans(db_path, args.backup_root, plans)
            if backup:
                print(f"备份: {backup}")
                print("预设已规范化。CC Switch 3.20.2 的 GUI 启用会重新改写 provider；")
                print("实际使用请运行 --activate NAME --apply。")
            else:
                print("没有需要写入的配置。")
        return 0
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
