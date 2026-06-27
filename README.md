# Workflow Checkpoint

跨会话任务管理——暂停、恢复、待办提醒，基于热度排序的注意力优先。

## 解决的问题

Claude Code 会话结束时任务状态丢失。下次启动忘了做到哪、还有什么没做、哪个最紧急。

**Workflow Checkpoint** 提供：
- 暂停时自动存盘（模型 提取进度 → 持久化到本地文件）
- 启动时自动提醒待办任务
- 热度排序——放越久越显眼（黄色 7 天，红色 14 天）
- `task-cli.py` 脚本——5 个命令管理完整生命周期

## 安装

```bash
cd ~/.claude/skills/workflow-checkpoint
python scripts/install.py
```

一步完成：
- 生成 SessionStart hook（Windows `.ps1` / Linux `.sh`）
- 注册到 `~/.claude/settings.json`
- 注入 `CLAUDE.md` 的 Workflow 引用（幂等）

## 使用

Scope 自动检测——无需传 `--global`/`--project`。脚本根据当前目录向上查找 `.git` 自动判断。

```bash
task-cli.py list                    # 查看待办（按热度排序，带颜色）
task-cli.py create <slug> <描述>     # 创建任务
task-cli.py pause <slug> [--step] [--next] [--plan]  # 暂停（写入 checkpoint）
task-cli.py resume <slug>           # 恢复
task-cli.py close <slug> [--yes]    # 关闭（--yes = 删除 spec + 任务目录）
```

## 热度排序

`list` 按 **热度** 排序——`progress.json` 的最后修改距今天数：

| 热度 | 颜色 | 含义 |
|------|:--:|------|
| < 7 天 | 默认 | 新鲜 |
| 7-13 天 | 黄色 | 一周以上，该关注了 |
| ≥ 14 天 | 红色 | 两周以上，紧急 |

纯时间排序，无状态偏见。放越久越排前面。

## 存储结构

```
~/.claude/global/workflows/
├── index.json              ← 脚本索引 [{slug, description, skill, event}]
├── fix-login/
│   ├── progress.json       ← 脚本状态 {ts, event, step, next, source_plan}
│   └── recovery.md         ← 模型 写的恢复上下文（纯 Markdown）
└── add-oauth/
    ├── progress.json
    └── recovery.md
```

- `index.json` — 脚本消费（`list`、hook）
- `progress.json` — 状态记录（覆盖写，不追加）
- `recovery.md` — 模型消费（暂停时 模型 写，恢复时模型读）

## 生命周期

```
Create → Active ⇄ Paused → Done（close --yes 删除目录）
```

| 状态 | event 字段 |
|------|-----------|
| 进行中 | `"active"` |
| 已暂停 | `"paused"` |
| 已完成 | 目录被删除 |

## 快速恢复流程

1. **SessionStart**：hook 自动注入待办任务到模型上下文
2. 模型提示："你有 2 个待办：fix-login(12d), add-oauth(3d)"
3. 用户说"恢复 fix-login"
4. 模型读 `recovery.md` → 从 "Next action" 继续

## 文件说明

| 文件 | 用途 |
|------|------|
| `SKILL.md` | 技能定义 + 完整的暂停/恢复协议 |
| `scripts/task-cli.py` | 任务管理引擎（5 命令 + JSON I/O + 热度排序） |
| `scripts/install.py` | 跨平台安装器（生成 hook + 注册 settings.json + 注入 CLAUDE.md） |
| `hooks/check-pending-tasks.ps1` | Windows SessionStart hook（由 install.py 生成） |

## 依赖

- Python 3.8+（仅标准库：`json`, `pathlib`, `argparse`, `datetime`, `time`, `shutil`）
- Claude Code（用于 SessionStart hook）

## 与 [Memory Lifecycle](https://github.com/Skydevourer-0/memory-lifecycle) 的关系

Workflow Checkpoint 管理**任务**（暂存/恢复），memory-lifecycle 管理**知识**（长期记忆）。两者互补但独立——数据目录分离（`global/workflows/` vs `global/memory/`），脚本分离（`task-cli.py` vs `memory-sync.py`）。
