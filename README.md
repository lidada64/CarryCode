# CarryCode

一个**只用 Python 标准库**的最小 agent loop：模型可以调用 `bash`，看结果，再决定下一步。

全部实现就是 `carrycode/__main__.py` 一个文件。没有配置分层、没有审批、没有会话——先把「模型 ↔ 命令」这条链路跑通。

## 跑起来

```powershell
cd CarryCode
.\run-carrycode.cmd "用 bash 统计当前目录下 .py 文件的数量，并给出结论"
```

等价写法（仓库根目录下）：

```powershell
python -m carrycode "用 bash 看看当前目录有什么文件"
```

## 配置

读仓库根的 `.env`，同名环境变量优先级更高：

```
ANTHROPIC_API_KEY=sk-...
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_MODEL=deepseek-v4-flash
```

`CARRYCODE_MODEL` 也认（兼容旧配置）。没有 `.env` 时直接用环境变量即可。

## 它怎么工作

1. 把任务、系统提示、`bash` 工具定义发给模型；
2. 模型返回 `tool_use` 就执行命令，把退出码和输出作为 `tool_result` 回传；
3. 重复直到 `stop_reason` 不是 `tool_use`，打印最终回答；最多 5 步。

assistant 的原始 content blocks（含 thinking）原样进历史——DeepSeek 的 Anthropic 兼容端点要求这样回传。

## 现在没有

- **审批**：命令会直接执行，只打印 `→ bash:` 让你看见；想拦就 Ctrl-C。
- REPL / 会话持久化 / 流式输出 / 其它工具 / 单测。
- Unix 语义：Windows 上命令走 `cmd`，`ls` 会失败，用 `dir` 或 `python`。

## 文件

```
CarryCode\
├─ carrycode\__main__.py   全部实现：配置、请求、bash、循环
├─ carrycode\__init__.py   版本号
├─ .env.example            密钥模板
├─ run-carrycode.cmd       一键启动，可在任意目录运行
└─ pyproject.toml          包元数据
```

## 已知限制

- 命令超时 120 秒，输出回给模型前截断到 12000 字符。
- Windows 上超时只杀直接子进程，孙进程可能残留。
- 模型可能不按约定调用工具；失败会作为工具结果回传，让它自己调整。

## 密钥红线

本仓库是公开的：`.env` 被 `.gitignore` 排除，永不入库；模板是 `.env.example`。
