# Workflow Checkpoint

跨 session 的任务检查点管理 —— 暂停、恢复、待办提醒，基于热度排序的注意力优先级。

## 解决的问题

Claude Code 在 session 之间会丢失任务状态。下一个 session 不记得上次在做什么、有哪些待办、哪个最紧急。

**Workflow Checkpoint 提供：**
- 暂停自动保存（模型写入检查点 → 脚本校验并持久化）
- SessionStart 自动提醒待办任务
- 热度排序 — 任务搁置越久越显眼（>=7 天黄色，>=14 天红色）
- `checkpoint.py` — 4 个命令覆盖完整生命周期

## 安装

```bash
python3 ~/.claude/skills/workflow-checkpoint/scripts/install.py
```

只在 `~/.claude/settings.json` 中注册一个 SessionStart hook：
```
python3 ~/.claude/skills/workflow-checkpoint/scripts/checkpoint.py list --hook
```

可以用 `--dry-run` 预览注册内容，不实际修改。

## 使用

Scope 自动检测，不需要手动指定 `--global` / `--project`。脚本从当前目录向上查找 `.git`，找到项目根目录则使用项目 scope，找不到则使用全局 scope。

```bash
# 查看待办（按热度排序，>=7天黄 >=14天红）
python3 ~/.claude/skills/workflow-checkpoint/scripts/checkpoint.py list

# SessionStart hook 输出（供 settings.json 中注册的 hook 调用）
python3 ~/.claude/skills/workflow-checkpoint/scripts/checkpoint.py list --hook

# 创建新任务（自动生成 id + .md 模板 + 扫描关联文档）
python3 ~/.claude/skills/workflow-checkpoint/scripts/checkpoint.py create "任务标题"

# 暂停/保存进度（校验 .md 内容后刷新时间戳）
python3 ~/.claude/skills/workflow-checkpoint/scripts/checkpoint.py pause <id> [--source-docs <路径>] [--skill <名称>]

# 关闭任务（dry-run 预览删除范围）
python3 ~/.claude/skills/workflow-checkpoint/scripts/checkpoint.py close <id>

# 确认关闭（校验 .md 后删除 .md 文件 + JSONL 记录 + 关联文档）
python3 ~/.claude/skills/workflow-checkpoint/scripts/checkpoint.py close <id> --yes
```

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
~/.claude/global/workflows/             （全局 scope，未找到 .git）
~/.claude/projects/<slug>/workflows/    （项目 scope，找到 .git）
├── workflows.jsonl                     ← 脚本独占写入，模型绝不直接操作
├── 20260629-100510-compare-skills.md   ← 模型编辑的恢复上下文
└── 20260629-100511-another-task.md
```

- `workflows.jsonl` — 每行一条 JSON 记录，6 个字段：`id`、`title`、`created`、`updated`、`skill`、`source_docs`
- `<id>.md` — 模型编写的恢复上下文：`## Completed`（>=100 字符）、`## Current`、`## Decisions`、`## Next`、`## Key Files`

没有状态机。文件存在即待办，文件删除即完成。

## 生命周期

```
Create → 工作中 → Pause（模型编辑 .md → 脚本校验 → 写入 JSONL）
                    ↓
              Resume（模型直接 Read .md → 加载 skill → 从 Next 继续）
                    ↓
              Close（dry-run 预览 → --yes 删除）
```

## 文件

| 文件 | 用途 |
|------|------|
| `SKILL.md` | Skill 定义 + 模型行为规则 |
| `scripts/checkpoint.py` | 命令行工具：list / create / pause / close + JSONL 读写 + 源文档扫描 |
| `scripts/install.py` | SessionStart hook 安装器（直接注册 python 命令，不生成中间脚本） |
| `scripts/migrate_v2.py` | 一次性迁移工具：v2 子目录格式 → v3 平面文件 |

## 依赖

- Python 3.8+（仅标准库：`json`、`pathlib`、`argparse`、`datetime`、`re`、`shutil`）
- Claude Code（使用 SessionStart hook）