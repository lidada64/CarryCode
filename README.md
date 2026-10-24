# CarryCode

一个**只用 Python 标准库**写的终端 code agent。边读 [MiniCode](https://github.com/) 源码边实现，所以刻意保持小：每个模块都能讲清楚，没有第三方依赖。

> **当前状态：S1 骨架施工前。**
> 已完成：`.env` 加载、三层配置兜底、密钥脱敏自检（`--check-config`）。
> 待建：事件流、工具定义与审批、回合循环、模型适配、CLI。顺序见工作区根目录的 `产品文档.md` 第 9.1 节施工单。

## 现在能跑的东西

```powershell
git clone https://github.com/lidada64/CarryCode.git
cd CarryCode
Copy-Item .env.example .env        # 填入你的 key
.\run-carrycode.cmd --check-config
```

输出形如（密钥只显示首尾，来源逐项标出）：

```
CarryCode 0.1.0
model      deepseek-v4-flash
base_url   https://api.deepseek.com/anthropic
api_key    sk-9…16ae
auth_token (未设置)
max_tokens 8192
timeout    120s

来源：
  model      ← .env:CARRYCODE_MODEL
  base_url   ← .env:ANTHROPIC_BASE_URL
  api_key    ← .env:ANTHROPIC_API_KEY
  ...
  .env       ← D:\...\CarryCode\.env

[ok] 配置可用。
```

也可以直接 `python -m carrycode --check-config`（不需要装包，仓库根就是运行目录）。

## 配置优先级

从高到低：

| 层级 | 位置 | 用途 |
|---|---|---|
| 1 | 命令行参数 | 临时覆盖（计划：`--model`） |
| 2 | 进程环境变量 | CI、一次性会话 |
| 3 | `CarryCode\.env` | **本机私有**，git 忽略 |
| 4 | `~/.carrycode/settings.json` | 个人偏好 |
| 5 | `~/.mini-code/settings.json` | 复用 MiniCode 现成配置兜底 |

`.env` 认的键：

```
ANTHROPIC_API_KEY      # 必填（或用 ANTHROPIC_AUTH_TOKEN）
ANTHROPIC_BASE_URL     # 默认 https://api.anthropic.com
ANTHROPIC_MODEL        # 或 CARRYCODE_MODEL
CARRYCODE_MAX_TOKENS   # 默认 8192
CARRYCODE_TIMEOUT      # 默认 120 秒
```

`CARRYCODE_*` 前缀是等价写法，优先级略高，便于临时覆盖。

## 目录

```
CarryCode\
├─ carrycode\           # 包：dotenv / config（已建）+ types / events / loop / tools / adapters / cli（S1）
├─ tests\               # 标准库 unittest
├─ demo\                # 三个验收场景的可复现脚本（S1）
├─ .env.example         # 密钥模板；.env 被 .gitignore 排除
├─ pyproject.toml       # 包元数据 + carrycode 命令入口
├─ run-carrycode.cmd    # Windows 一键启动，可在任意目录运行
└─ README.md
```

## 开发

```powershell
python -m unittest discover -s tests -t .
```

不装任何包也能跑（纯标准库，Python 3.10+）。

## 密钥红线

本仓库是**公开**的。

- `.env` 在 `.gitignore` 里，永不入库；模板是 `.env.example`。
- 任何输出都脱敏（`sk-9…16ae`），`--check-config` 也不例外。
- 提交前自查：`git grep -I "sk-"` 应该什么都搜不到。

## 设计来源

对照表（实现 ↔ MiniCode 源码）与验收标准都在 `产品文档.md`，它放在工作区根目录，**不在本仓库内**——产品文档记录的是整个学习项目的决策，代码仓库只放代码。
