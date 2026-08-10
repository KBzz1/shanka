---
name: agent-md-maintenance
description: Use when creating, modifying, or reviewing AGENTS.md files in a project (CLAUDE.md is a symlink to AGENTS.md)
context: fork
---

# AGENTS.md 维护

## Overview

AGENTS.md 是 Claude Code 的持久化上下文文件，影响每个会话的行为。写好 AGENTS.md 是提升 Claude Code 输出质量的最高杠杆点。

**核心原则：** 内容必须 universally applicable；稀疏克制；不替 linter 干活；用指针而非复制代码。

**重要：** 本项目中 `CLAUDE.md` 已统一替换为指向同目录 `AGENTS.md` 的符号链接，只需维护 `AGENTS.md` 一个文件。

## When to Use

触发条件：
- 用户说「新建/修改 AGENTS.md」「给 XX 目录加规则」
- 用户说「检查 AGENTS.md 有没有问题」
- `/agent-md-maintenance` 被调用

不触发：
- 修改不相关的配置文件
- 纯代码实现任务（除非用户明确要求更新 AGENTS.md）

## AGENTS.md 内容规范

此规范综合自 [HumanLayer CLAUDE.md 最佳实践](https://www.humanlayer.dev/blog/writing-a-good-claude-md)。

### 三要素：WHAT / WHY / HOW

| 维度 | 覆盖内容 |
|------|----------|
| WHAT | 技术栈、项目结构、各模块职责 |
| WHY | 项目目的、各组件功能定位 |
| HOW | 工具约定（包管理器、构建命令、测试命令） |

### 硬性约束

- **行数上限 150 行**。超过此限模型指令遵从度下降。超长内容拆分到独立文档，用 progressive disclosure。
- **universally applicable**。AGENTS.md 进入每个会话，所有内容必须对所有任务都相关。模块特定规则放子目录 AGENTS.md。
- **不写代码风格规则**。风格交给 linter/格式化工具（Stop hook 或 slash command）。LLM 是上下文学习者，会从代码库中自动学习模式。
- **用 file:line 指针**。引用权威源码位置而非复制代码片段。复制会过时，指针保持准确。
- **每行都是刻意保留的**。不自动生成，不批量导入。AGENTS.md 里一行坏代码比普通代码里一行坏代码的危害大得多。

### 不该出现的

- 数据库 schema 规则（如果你经常做前端功能）
- 代码格式化规范（linter 的工作）
- 过期的代码片段
- 非通用指令

## AGENTS.md 层级规则

此规范来自 [Mintlify Claude Code 最佳实践](https://mintlify.wiki/shanraisshan/claude-code-best-practice/best-practices/memory)。

| 层级 | 文件 | 职责 | 加载 |
|------|------|------|------|
| 全局 | `~/.claude/CLAUDE.md` | 个人跨项目偏好 | 所有会话自动加载 |
| 根级 | `./AGENTS.md` | 仓库级约定 | 祖先上载（启动时加载） |
| 子目录 | `./<dir>/AGENTS.md` | 组件级模式 | 后代懒载（进入该目录时加载） |
| 个人 | `AGENTS.local.md` | 本地偏好 | 需手动加载，不提交 |

**规则：**
- 兄弟目录的 AGENTS.md 永不加载。在 `frontend/` 工作不会加载 `backend/AGENTS.md`。
- 架构/构建/测试命令放根级。框架特定模式放子目录级。
- 新增子目录 AGENTS.md 时，只在内容与根级不同时才写，不复制根级内容。

## CLAUDE.md 符号链接规则

- `CLAUDE.md` 统一为指向同目录 `AGENTS.md` 的符号链接（`ln -s AGENTS.md CLAUDE.md`）。
- 只需维护 AGENTS.md 一个文件，CLAUDE.md 自动解析到相同内容。
- 新建子目录 AGENTS.md 后 → 在该目录执行 `ln -sf AGENTS.md CLAUDE.md` 创建或更新符号链接。
- 扫描范围：整个仓库，排除 `.git/`、`.worktrees/`、`__pycache__/`、`node_modules/`、`jetson_data/` 等。

## Quick Reference

| 检查项 | 标准 |
|--------|------|
| 行数 | ≤ 150 |
| 内容范围 | 全仓库通用 |
| 代码风格 | 无（交给 linter） |
| 代码片段 | 用 file:line 指针替代 |
| 符号链接 | CLAUDE.md → AGENTS.md（同目录相对符号链接） |
| 标题匹配 | 文件名与 `#` 标题一致 |

## 不做什么

- 不做实时同步（用户主动调用 `/agent-md-maintenance` 才执行）。
- 不强制修改已有内容（只补齐缺失的 AGENTS.md 或符号链接，不覆盖已有文件）。
- 不机械生成全新的 AGENTS.md（内容应由用户意图驱动，模板仅供参考）。

## Common Mistakes

| 错误 | 修复 |
|------|------|
| AGENTS.md 超过 150 行 | 拆分为 root + 子目录，或外链到 `docs/` 下的文件 |
| 在根级 AGENTS.md 里写 `src/auth/` 的细节 | 移到 `src/auth/AGENTS.md` |
| 复制代码片段描述 API | 改为 `src/api.py:45-60` 指针 |
| 加了 ESLint 规则 | 删除交给 `.eslintrc` |
| 新建子目录 AGENTS.md 后忘记创建符号链接 | 执行 `ln -sf AGENTS.md CLAUDE.md` |
| 同时维护 CLAUDE.md 和 AGENTS.md 两个文件 | 只需维护 AGENTS.md，CLAUDE.md 是符号链接 |
| 子目录 AGENTS.md 复制了根级内容 | 只写该子目录特有的规则 |
