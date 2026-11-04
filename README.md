# CarryCode

一个**只用 Python 标准库**的最小 agent loop：模型可以调用 5 个工具（`bash` / `read_file` / `write_file` / `edit_file` / `glob`），看结果，再决定下一步。

全部实现就是 `carrycode/__main__.py` 一个文件。没有配置分层、没有审批、没有落盘的会话——历史和上下文只在内存里。

## 跑起来

```powershell
cd CarryCode
.\run-carrycode.cmd
```

不带任务参数就进对话框，一行一条任务：

```
● deepseek-v4-flash  ·  https://api.deepseek.com/anthropic
输入任务回车发送；/help 看命令，/exit 退出。

› 用 glob 找出所有 .py 文件，读出最大的那个的前 20 行
→ glob: {"pattern": "**/*.py"}
← ok · 156 字符
…

› 把刚才那个文件第 3 行的注释改成中文
（接着上一轮的上下文，不用重新说一遍）
```

| 命令 | 作用 |
|---|---|
| `/help` | 看命令 |
| `/new` | 清空历史，开一段新对话 |
| `/exit`（或 Ctrl-D） | 退出 |
| Ctrl-C | 中断当前这一轮，历史回到中断前 |

历史一直留在同一个 `messages` 里，所以后面几轮能接着前面聊；请求开头那段原样不动，也能接着吃前缀缓存。

也可以一次性跑完就退出：

```powershell
python -m carrycode "看看当前目录有什么文件"
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

1. 把任务、系统提示、5 个工具的定义发给模型；
2. 模型返回 `tool_use` 就交给 `dispatch()`：查名字、跑函数、把结果变成 `tool_result` 回传；
3. 重复直到 `stop_reason` 不是 `tool_use`，打印最终回答；最多 5 步。

assistant 的原始 content blocks（含 thinking）原样进历史——DeepSeek 的 Anthropic 兼容端点要求这样回传。

### 工具表为什么是常量

`TOOLS` 是模块级字典（名字 → 说明 + 函数），`TOOL_SCHEMAS` 在加载时算一次，之后每次请求原样发出去。工具顺序、说明文字都不随请求变化，所以请求开头那段逐字相同，服务端的前缀缓存才认得出来——改一个字段，就从那个字往后全部重算。

要停用某个工具，在 `dispatch()` 里拒绝并返回结果，不要从 `TOOLS` 里删：删掉会改变发出去的说明，等于每次请求都换个开头。

## 工具

| 工具 | 干什么 | 关键参数 |
|---|---|---|
| `bash` | 执行一条 shell 命令，返回 exit_code / stdout / stderr | `command` |
| `read_file` | 读文件的一段，每行带行号 | `path`、`offset`、`limit`（默认 200 行） |
| `write_file` | 整份写入（覆盖），父目录自动创建 | `path`、`content` |
| `edit_file` | 把一段**唯一出现**的原文替换掉 | `path`、`old_string`、`new_string`、`replace_all` |
| `glob` | 按模式找文件，按路径排序 | `pattern`、`limit`（默认 100） |

`edit_file` 的 `old_string` 必须和文件里的字符完全一致（含缩进和换行）；找不到、或者出现多次，都直接作为错误结果返回，让模型自己补上下文——不去猜改哪一处。CRLF 的文件会自动保住原来的行尾。

`glob` 默认跳过 `.git`、`node_modules`、`__pycache__`、`dist` 这类目录。

## 现在没有

- **审批**：命令、写文件都会直接执行，只打印 `→ 工具名: 参数` 让你看见；想拦就 Ctrl-C。
- 会话持久化（历史只在内存里，退出就没了）/ 流式输出 / 单测 / 大结果外置到文件。
- Unix 语义：Windows 上命令走 `cmd`，`ls` 会失败，用 `dir` 或 `python`。

## 文件

```
CarryCode\
├─ carrycode\__main__.py   全部实现：配置、请求、5 个工具与分发、循环
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
