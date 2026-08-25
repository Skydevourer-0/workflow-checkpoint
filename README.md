# Workflow Checkpoint

跨 session 的任务检查点管理 —— 暂停、恢复、待办提醒，基于热度排序的注意力优先级。

## 解决的问题

Claude Code 在 session 之间会丢失任务状态。下一个 session 不记得上次在做什么、有哪些待办、哪个最紧急。

**Workflow Checkpoint 提供：**
- 暂停自动保存（模型写入检查点 → 脚本校验并持久化）
- SessionStart 自动提醒待办任务
- 热度排序 — 任务搁置越久越显眼（>=7 天黄色，>=14 天红色）
- 流级归档 — 完成的子流折叠出活跃 `.md` 进 `<id>_history.md`，长任务不膨胀
- `checkpoint.py` — 5 个命令覆盖完整生命周期

## 安装

数据与命令在 Claude Code / Codex 双端一致，安装器分端注册 SessionStart hook：

**Claude Code**（写 `~/.claude/settings.json`，matcher `startup|resume|clear|compact`）：

```bash
python ~/.cc-switch/skills/workflow-checkpoint/.claude/install.py
```

**Codex**（写 `$CODEX_HOME/hooks.json`，默认 `~/.codex/hooks.json`）：

```bash
python ~/.cc-switch/skills/workflow-checkpoint/.codex/install.py
```

然后在 Codex 会话中运行 `/hooks` 批准 workflow-checkpoint SessionStart hook；重装后需重新批准（hook 按命令哈希 trust）。`scripts/install.py` 是兼容旧入口，自动检测环境转发。均支持 `--dry-run` 预览。

Hook 只注入不阻断：会话启动时把待办任务作为 `additionalContext` 输出（截断至 1200 字符），始终 exit 0。Codex `/clear` 会重新触发 SessionStart hook（matcher 含 `clear`），待办会重新注入。

## 使用

Scope 自动检测，不需要手动指定 `--global` / `--project`。脚本从当前目录向上查找 `.git`，找到项目根目录则使用项目 scope，找不到则使用全局 scope。

```bash
# 查看待办（按热度排序，>=7天黄 >=14天红）
python3 ~/.cc-switch/skills/workflow-checkpoint/scripts/checkpoint.py list

# SessionStart hook 输出（供 settings.json 中注册的 hook 调用）
python3 ~/.cc-switch/skills/workflow-checkpoint/scripts/checkpoint.py list --hook

# 创建新任务（--note 必填，自动生成 id + .md 模板 + 扫描关联文档）
python3 ~/.cc-switch/skills/workflow-checkpoint/scripts/checkpoint.py create "任务标题" --note "上下文：因何而起、要做什么"

# 暂停/保存进度（校验 .md 内容后刷新时间戳；超长段落会打印非阻塞告警）
python3 ~/.cc-switch/skills/workflow-checkpoint/scripts/checkpoint.py pause <id> [--source-docs <路径>] [--skill <名称>]

# 折叠完成的子流（dry-run 预览）
python3 ~/.cc-switch/skills/workflow-checkpoint/scripts/checkpoint.py archive-stream <id> <stream>
# 确认折叠（删除标记正文 → 写一行摘要进 <id>_history.md → ## Completed 加指针）
python3 ~/.cc-switch/skills/workflow-checkpoint/scripts/checkpoint.py archive-stream <id> <stream> --yes

# 关闭任务（dry-run 预览归档范围）
python3 ~/.cc-switch/skills/workflow-checkpoint/scripts/checkpoint.py close <id>
# 确认关闭（校验 .md → 归档 .md + <id>_history.md 到 archived/，记录移入 archive.jsonl）
python3 ~/.cc-switch/skills/workflow-checkpoint/scripts/checkpoint.py close <id> --yes
```

### 流标记（archive-stream 用）

完成的子流用 HTML 注释标记包裹，再归档。标记必须段落内闭合（Current 或 Next 之一，不跨 `## ` 边界）：

```
<!-- stream:start:<name> -->
...流叙事...
<!-- stream:end:<name> -->
```

`<name>` 匹配 `[a-z0-9-]+`。`create` 自动播种第一对（`initial`，包裹 note）。跨 Current/Next 的流须分两次归档（中间隔着 `## Decisions`）。详见 `SKILL.md`。

## 热度排序

`list` 按 `updated` 时间戳距今的天数降序排列：

| 热度 | 颜色 | 含义 |
|------|:--:|------|
| < 7 天 | 默认 | 新鲜 |
| 7-13 天 | 黄色 | 超过一周，值得关注 |
| >= 14 天 | 红色 | 超过两周，紧急 |

纯时间排序。越久越靠前。

## 存储结构

```
~/.cc-switch/workflows/global/          （全局 scope，未找到 .git 或位于 ~/.claude/、~/.cc-switch/skills/）
~/.cc-switch/workflows/projects/<slug>/ （项目 scope，找到 .git）
├── workflows.jsonl                     ← 脚本独占写入，模型绝不直接操作
├── 20260629-100510-compare-skills.md   ← 模型编辑的恢复上下文
├── 20260629-100510-compare-skills_history.md  ← archive-stream 写的流摘要（懒创建）
├── archived/                           ← close 后的 .md / _history.md 移入此处
└── archive.jsonl                       ← close 后的记录移入此处（status=closed）
```

- `workflows.jsonl` — 每行一条 JSON 记录，6 个字段：`id`、`title`、`created`、`updated`、`skill`、`source_docs`
- `<id>.md` — 模型编写的恢复上下文：`## Completed`（>=100 非空白字符 **或** `History: <id>_history.md` 指针行）、`## Current`、`## Decisions`、`## Next`、`## Key Files`
- `<id>_history.md` — archive-stream 产出的流摘要，每行一条（`- <stream>: <summary> @<commit> [mem:<slug>]`）；脚本只追加不解析
- close 不删除任何文件：`.md` 与 `_history.md` 移入 `archived/`，记录移入 `archive.jsonl`（`status=closed` + `closed_at`），`source_docs` 原地保留

## 生命周期

```
Create → 工作中 → Pause（模型编辑 .md → 脚本校验 → 写入 JSONL → 超长告警）
                    ↓
              子流完成 → archive-stream（标记正文删除 → 摘要进 _history.md → .md 瘦身）
                    ↓
              Resume（模型直接 Read .md → 加载 skill → 从 Next 继续；可选读 _history.md）
                    ↓
              Close（dry-run 预览 → --yes 归档：.md + _history.md → archived/，记录 → archive.jsonl）
```

## 文件

| 文件 | 用途 |
|------|------|
| `SKILL.md` | Skill 定义 + 模型行为规则 |
| `scripts/checkpoint.py` | 命令行工具：list / create / pause / archive-stream / close + JSONL 读写 + 源文档扫描 |
| `.claude/install.py` | Claude Code 安装器：写 `~/.claude/settings.json` SessionStart hook |
| `.codex/install.py` | Codex 安装器：写 `$CODEX_HOME/hooks.json` SessionStart hook |
| `scripts/install.py` | 兼容旧入口：检测环境并转发到上述安装器 |
| `scripts/migrate_v2.py` | 一次性迁移工具：v2 子目录格式 → v3 平面文件 |

## 依赖

- Python 3.8+（仅标准库：`json`、`pathlib`、`argparse`、`datetime`、`re`、`shutil`、`subprocess`）
- Claude Code / Codex（双端 SessionStart hook，数据与命令一致）
