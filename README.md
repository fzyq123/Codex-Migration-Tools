# Codex Migration Tools

一组面向 macOS 的 Codex 本地数据迁移工具，用于统一不同 provider 创建的历史任务，并让 CC Switch 中兼容 OpenAI Responses API 的第三方配置与 OpenAI Official 共用 Codex 内建的 `openai` 历史 bucket。

项目包含两个相互独立、可以组合使用的命令行工具：

- `codex_history_migrator.py`：迁移 Codex 历史任务，并清理跨 provider 不兼容的 Responses items。
- `ccswitch_codex_openai_compat.py`：规范化或激活 CC Switch 中原生支持 Responses API 的 Codex 配置。

两个工具默认均为只读预览。只有显式添加 `--apply` 才会写入本地数据。

> [!WARNING]
> 这些工具会在 `--apply` 模式下修改 Codex 或 CC Switch 的本地数据库和配置。请先阅读预览结果，并保留自动生成的备份。不要将备份、`auth.json`、数据库快照或会话 JSONL 上传到公开仓库。

## 适用场景

当 Codex 任务分别由官方 OpenAI provider 和第三方 Responses-compatible provider 创建时，不同的 `model_provider` 可能导致历史任务分桶，跨 provider 续聊还可能遇到以下问题：

- reasoning 或 compaction item 属于原 provider，无法在新 provider 上回放；
- message、function call 等可移植 item 带有与目标类型不匹配的 ID；
- CC Switch 将带 API key 的第三方配置写成自定义 provider，导致历史再次分桶；
- 切换供应商后仍残留上一配置的模型目录。

本项目将兼容配置统一到 Codex 内建的 `openai` provider，同时保留第三方 `openai_base_url`，并对已有历史任务执行必要的兼容性清理。

## 环境要求

- macOS；
- Python 3.9 或更高版本；
- 已安装并使用 Codex；
- 使用 CC Switch 功能时，需要本机已有 CC Switch 数据库；
- 第三方直连配置的 `meta.apiFormat` 必须是 `openai_responses`。

项目只使用 Python 标准库，不需要安装额外依赖。

CC Switch 兼容逻辑是针对 3.20.2 的配置行为编写和验证的。其他版本的数据结构或切换行为可能不同，请先使用只读命令确认结果。

## 获取与测试

```bash
git clone https://github.com/fzyq123/Codex-Migration-Tools.git codex-migration
cd codex-migration
python3 -m unittest discover -s . -p 'test_*.py'
```

查看两个工具的完整参数：

```bash
python3 codex_history_migrator.py --help
python3 ccswitch_codex_openai_compat.py --help
```

## 推荐工作流程

### 1. 检查 CC Switch 配置

列出所有 Codex 配置及兼容状态，不写入任何内容：

```bash
python3 ccswitch_codex_openai_compat.py --list
```

第三方 provider 只有在 CC Switch 数据库中的 `meta.apiFormat` 为 `openai_responses` 时才能通过本工具直连。以下配置会被拒绝：

- `openai_chat`；
- `anthropic`；
- 缺失或未知的 `meta.apiFormat`；
- 依赖自定义请求头、查询参数或嵌套认证表的配置。

这些配置仍需要 CC Switch 本地路由进行协议转换，不能仅通过 `openai_base_url` 直连。

### 2. 预览并规范化第三方配置

使用 CC Switch 中显示的精确名称或 provider ID：

```bash
python3 ccswitch_codex_openai_compat.py --provider "PROVIDER_NAME"
```

确认预览无误后写入：

```bash
python3 ccswitch_codex_openai_compat.py \
  --provider "PROVIDER_NAME" \
  --apply
```

也可以预览或处理所有第三方配置：

```bash
python3 ccswitch_codex_openai_compat.py --all-third-party
python3 ccswitch_codex_openai_compat.py --all-third-party --apply
```

### 3. 激活一个配置

激活前，请先在 CC Switch 中关闭 **Codex 本地路由**。

先预览：

```bash
python3 ccswitch_codex_openai_compat.py --activate "PROVIDER_NAME"
```

再执行：

```bash
python3 ccswitch_codex_openai_compat.py \
  --activate "PROVIDER_NAME" \
  --apply
```

激活第三方配置时，工具会：

1. 将 live Codex 配置写成内建 `openai` provider 与对应的 `openai_base_url`；
2. 将所选配置保存的 API key 写入 Codex 的 API-key 登录文件；
3. 写入或清理 CC Switch 模型目录；
4. 更新 CC Switch 当前配置标记；
5. 在写入前备份数据库和相关 live 文件。

完成后重启 Codex。对于已经由本工具规范化的第三方配置，不要再点击 CC Switch 卡片中的“启用”，否则 CC Switch 3.20.2 可能重新生成自定义 provider 配置。

切回 OpenAI Official：

```bash
python3 ccswitch_codex_openai_compat.py \
  --activate "OpenAI Official" \
  --apply
```

该操作会恢复 CC Switch 保存的官方配置和 ChatGPT 登录信息。

### 4. 检查 Codex 历史任务

列出最近 20 个用户任务及迁移状态：

```bash
python3 codex_history_migrator.py --list
```

指定其他数量：

```bash
python3 codex_history_migrator.py --list 50
```

每个任务会显示为以下状态之一：

- `已完成/无需处理`；
- `仅需迁移 provider`；
- `仅需清理 item`；
- `需要迁移 provider + item`；
- `解析错误`。

### 5. 预览并迁移历史任务

按任务 ID 选择一个或多个任务：

```bash
python3 codex_history_migrator.py \
  --thread-id THREAD_ID
```

确认预览后执行：

```bash
python3 codex_history_migrator.py \
  --thread-id THREAD_ID \
  --apply
```

处理最近若干用户任务：

```bash
python3 codex_history_migrator.py --latest 5
python3 codex_history_migrator.py --latest 5 --apply
```

处理全部用户任务：

```bash
python3 codex_history_migrator.py --all
python3 codex_history_migrator.py --all --apply
```

只迁移指定来源 provider：

```bash
python3 codex_history_migrator.py \
  --all \
  --from-provider "SOURCE_PROVIDER" \
  --apply
```

默认情况下，`--latest` 和 `--all` 不包含 subagent、guardian 等内部任务。如确有需要，可以添加 `--include-internal`。

## `--force-drop-opaque`

如果任务已经被其他工具提前改成 `openai`，原 provider 信息已经丢失，但续聊仍出现 reasoning 或 compaction 相关错误，可以先针对明确的任务进行预览：

```bash
python3 codex_history_migrator.py \
  --thread-id THREAD_ID \
  --force-drop-opaque
```

确认后再添加 `--apply`：

```bash
python3 codex_history_migrator.py \
  --thread-id THREAD_ID \
  --force-drop-opaque \
  --apply
```

不要对全部官方 OpenAI 任务无差别使用该选项。它会删除已保存的 reasoning 和 compaction opaque state；可见对话会保留，但继续对话时的上下文质量可能受到影响。

## 迁移规则

历史迁移器会同时处理 rollout JSONL 和最高版本的 `state_*.sqlite`：

- 将选中来源的 `model_provider` 统一为 `openai`；
- 跨 provider 迁移时删除 provider-specific 的 reasoning 和 compaction items；
- 对 message、function call 等可移植 items，仅移除类型不匹配的 `id`；
- 保留消息内容、`call_id` 和工具输出；
- 同时扫描 `sessions` 与 `archived_sessions`；
- 已经处理完成的任务不会被重复改写。

脚本不会修改 Codex 的 `config.toml` 或 CC Switch 设置。

## 数据位置与自定义路径

默认位置：

- Codex 数据目录：`$CODEX_HOME`，未设置时使用 `~/.codex`；
- CC Switch 数据库：`~/.cc-switch/cc-switch.db`；
- 自动备份：项目目录下的 `backups/`。

可以显式指定其他位置：

```bash
python3 codex_history_migrator.py \
  --codex-home /path/to/codex-home \
  --state-db /path/to/state.sqlite \
  --backup-dir /path/to/backups \
  --list

python3 ccswitch_codex_openai_compat.py \
  --db /path/to/cc-switch.db \
  --codex-home /path/to/codex-home \
  --backup-root /path/to/backups \
  --list
```

## 安全机制

- 默认 dry-run，只有 `--apply` 才会写入；
- 修改前自动创建 SQLite 一致性快照；
- CC Switch 激活会备份原始 `config.toml`、`auth.json` 和模型目录文件；
- JSONL 解析失败时拒绝迁移；
- 修改前后检查 SQLite 完整性；
- 使用 SQLite 事务和原子文件替换；
- 检测并拒绝修改仍被 Codex writer lock 占用的任务；
- 重复执行具有幂等性；
- API key 不会作为正常命令输出显示。

## 隐私提示

自动备份可能包含完整对话、任务标题、本机路径、provider 配置、API key 或登录 token。`backups/` 已被 `.gitignore` 排除，但仍应按敏感数据管理：

- 不要上传、提交或分享整个 `backups/` 目录；
- 不要使用 `git add -f` 强制添加备份文件；
- 不要公开分享未经检查的命令输出或错误日志；
- 发布前使用 `git status` 和密钥扫描工具检查实际暂存内容；
- 如果凭证曾进入 Git 历史或被公开，应先撤销并轮换凭证，删除文件本身并不能使凭证重新安全。

## 备份与恢复

每次实际写入都会创建带时间戳的备份目录。manifest 记录源文件、哈希和修改对象，数据库快照及 rollout 副本保留修改前的数据。

当前版本没有自动 `--restore` 命令。需要恢复时，请先退出 Codex、关闭 CC Switch 本地路由，并根据对应备份目录中的 `manifest.json` 手动还原文件或数据库。恢复前建议先复制当前状态，避免覆盖之后产生的新任务或配置。

## 项目文件

```text
.
├── README.md
├── ccswitch_codex_openai_compat.py
├── codex_history_migrator.py
├── test_ccswitch_codex_openai_compat.py
└── test_codex_history_migrator.py
```

## 开源许可证

本项目采用 MIT License，详情参见 LICENSE。

## 致谢与参考

- `makoMakoGo/fish-claude` 中 `codex-provider-history-migrator` 的 provider 对齐思路；
- OpenAI Responses API 的 item 类型与跨 provider opaque state 行为；
- OpenAI Codex 关于跨 provider Responses item 兼容性的相关讨论。

本项目是独立的社区工具，不隶属于 OpenAI 或 CC Switch。运行前请检查预览结果，并自行承担修改本地数据的风险。
