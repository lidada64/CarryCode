# CarryCode

一个**只用 Python 标准库**的最小 agent loop：模型可以调用 6 个工具（`bash` / `read_file` / `write_file` / `edit_file` / `glob` / `grep`），看结果，再决定下一步。

没有配置分层、没有落盘的会话——历史和上下文只在内存里。入口在 `carrycode/__main__.py`，工具都在 `carrycode/tools/` 下，一个工具一个文件，审批在 `carrycode/permissions.py`。

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

1. 把任务、系统提示、6 个工具的定义发给模型；
2. 模型返回 `tool_use` 就交给 `dispatch()`：查名字、跑函数、把结果变成 `tool_result` 回传；
3. 重复直到 `stop_reason` 不是 `tool_use`，打印最终回答；最多 5 步。

assistant 的原始 content blocks（含 thinking）原样进历史——DeepSeek 的 Anthropic 兼容端点要求这样回传。

### 回答的渲染

模型的回答是 markdown（标题、列表、表格、代码块），直接 `print` 出去很难读，所以统一走 `carrycode/render.py`：装了 rich 就排成终端样式，没装就退化成纯文本（标题加下划线、去掉行内标记、代码块缩进），两条路都不输出 ANSI，老版 PowerShell 里也不会出现 `←[1m`。

### 会话结束的报告

退出（`/exit`、Ctrl-D、Ctrl-C，或者一次性任务跑完/失败）时会走一次 `session_end`：`carrycode/hooks.py` 喊一声，`carrycode/session_report.py` 把本次统计拼成 markdown 再渲染出来。

```
# CarryCode 会话报告

| 项目 | 值 |
| --- | --- |
| 模型 | deepseek-v4-flash |
| 任务轮数 | 3 |
| 模型请求 | 7 |

## 算力

| 输入 tokens | 输出 tokens | 合计 |
| ---: | ---: | ---: |
| 18,432 | 2,310 | 20,742 |

## 工具调用

| 工具 | 次数 |
| --- | ---: |
| bash | 2 |
```

装了 `rich` 就按终端样式渲染（`pip install -e .[render]`）；没装就退化成带 ANSI 的纯文本，不报错、不影响退出——默认依赖仍然只有标准库。钩子只做通知：handler 自己出错会被就地吞掉，不会把退出流程一起弄挂。

### 工具表为什么是常量

工具清单由 `tools/__init__.py` 装配：按固定顺序登记，`TOOL_SCHEMAS` 在加载时算一次，之后每次请求原样发出去。工具顺序、说明文字都不随请求变化，所以请求开头那段逐字相同，服务端的前缀缓存才认得出来——改一个字段，就从那个字往后全部重算。测试里有一条断言盯着这个顺序（`tests/test_architecture.py`）。

要停用某个工具，在 `dispatch()` 里拒绝并返回结果，不要从册子里删：删掉会改变发出去的说明，等于每次请求都换个开头。

## 审批

写文件、改文件、执行命令之前都会问一句；看文件、找文件不问。

| 用法 | 行为 |
|---|---|
| 默认 | 有副作用的调用先问 `[y/a/N/d]` |
| `--readonly` | 写和执行一律拒，一个字节都不改 |
| `--yes` | 常规调用不问；**不可逆命令仍然问一次**（只给 `y/N`） |
| `--allow-outside` | 放开工作区边界（写和读都放） |

```
要执行一条不可撤销的命令，允许吗？
  命令: git push
  会失去：这次提交留在本机的独占权
  能不能恢复：能，但要别人配合；CI 可能已经跑过
  影响范围：远端仓库，不只是本机
  更安全的做法：先 git log 看一眼要推什么
  [y/N]
```

三类东西**不问、直接拒**：禁令（`sudo`、`rm -rf`、`format`、`shutdown`）、禁区文件（`.env`、`~/.ssh`、`*.pem`；`.env.example` 可以读）、写越界（只有 `--allow-outside` 能开）。

`a` / `d` 记住的是"这一类"：`write_file` 按工具名，`bash` 按命令的第一个词（`git`、`npm`）。**不可逆命令和出工作区的读不吃这套记忆**，每次都要问。

判定全在 `permissions.py`；工具只负责说"我有多危险"（`RISK` / `inspect()` / `preview()`），不决定问不问。禁令表防手滑，不防蓄意绕——`python -c "import shutil; shutil.rmtree('.')"` 抓不到，这点要知道。

## 工具

| 工具 | 干什么 | 关键参数 |
|---|---|---|
| `bash` | 执行一条 shell 命令，返回 exit_code / stdout / stderr | `command` |
| `read_file` | 读文件的一段，每行带行号 | `path`、`offset`、`limit`（默认 200 行） |
| `write_file` | 整份写入（覆盖），父目录自动创建 | `path`、`content` |
| `edit_file` | 把一段**唯一出现**的原文替换掉 | `path`、`old_string`、`new_string`、`replace_all` |
| `glob` | 按模式找文件，按路径排序 | `pattern`、`limit`（默认 100） |
| `grep` | 按正则搜文件内容，返回「路径:行号: 内容」 | `pattern`、`path`、`glob`、`limit`（默认 100） |

`edit_file` 的 `old_string` 必须和文件里的字符完全一致（含缩进和换行）；找不到、或者出现多次，都直接作为错误结果返回，让模型自己补上下文——不去猜改哪一处。CRLF 的文件会自动保住原来的行尾。

`glob` 和 `grep` 默认跳过 `.git`、`node_modules`、`__pycache__`、`dist` 这类目录；`grep` 还会跳过二进制文件和超过 2MB 的文件。

## 现在没有

- 会话持久化（历史只在内存里，退出就没了）/ 流式输出 / 大结果外置到文件。
- Unix 语义：Windows 上命令走 `cmd`，`ls` 会失败，用 `dir` 或 `python`。

## 跑测试

```powershell
python -m unittest discover -s tests -t .
```

现在有三组：架构约束（工具层不许反向 import、工具顺序固定、每个工具都有说明和参数）、审批判定（假 asker，断言问没问、允不允许）、会话报告与钩子（不联网，断言统计和 markdown 拼得对）。

## 文件

```
CarryCode\
├─ carrycode\
│  ├─ __init__.py          版本号
│  ├─ __main__.py          入口：配置、系统提示、请求、回合循环、对话框、审批的显示
│  ├─ permissions.py       审批：三档模式 + 边界 + 记忆，十步判定链（不 print、不 input）
│  ├─ hooks.py             生命周期钩子：session_start / session_end，出错不影响主流程
│  ├─ render.py            回答的 markdown 渲染：rich 可用就排版，否则纯文本兜底
│  ├─ session_report.py    会话统计 → markdown → 交给 render.py 渲染
│  └─ tools\
│     ├─ __init__.py       装配：装哪几个工具、按什么顺序（唯一能改顺序的地方）
│     ├─ tool.py           花名册 + dispatch（不认识任何具体工具）
│     ├─ base.py           公共件：路径、文本读写、参数兜底、禁区表（不 import 任何东西）
│     ├─ commands.py       命令串的知识：切词、找命令名、禁令表、不可逆表
│     ├─ bash.py           每个工具一个文件，里面是 NAME / SCHEMA / run 三件套
│     ├─ read_file.py      外加可选的 RISK / PATHS / preview / inspect
│     ├─ write_file.py
│     ├─ edit_file.py
│     ├─ glob.py
│     └─ grep.py
├─ tests\
│  ├─ __init__.py
│  ├─ test_architecture.py 架构约束：不许反向 import、顺序固定、安全账本
│  ├─ test_permissions.py  审批：全程无终端，用假 asker 断言"问没问、允不允许"
│  ├─ test_render.py       渲染：纯文本兜底的结构与"不出 ANSI"
│  └─ test_session_report.py 会话报告与钩子：不联网，只断言拼出来的东西对不对
├─ docs\permission-design.md  审批的设计与决策记录（D1–D6）
├─ .env.example            密钥模板
├─ run-carrycode.cmd       一键启动，可在任意目录运行
└─ pyproject.toml          包元数据
```

加一个工具 = 新建 `tools/<名字>.py`（`NAME` / `SCHEMA` / `run` 三件套 + 一行 `RISK`）+ 在 `tools/__init__.py` 的 `_MODULES` 末尾加一行，其它文件一个字不用动。

忘了写 `RISK` 会被测试抓住；而且 `register()` 的缺省值落在"要问"那一边，不会静默放行。

加一个工具 = 新建 `tools/<名字>.py`（`NAME` / `SCHEMA` / `run`）+ 在 `tools/__init__.py` 的 `_MODULES` 末尾加一行，其它文件一个字不用动。

## 已知限制

- 命令超时 120 秒，输出回给模型前截断到 12000 字符。
- Windows 上超时只杀直接子进程，孙进程可能残留。
- 模型可能不按约定调用工具；失败会作为工具结果回传，让它自己调整。

## 密钥红线

本仓库是公开的：`.env` 被 `.gitignore` 排除，永不入库；模板是 `.env.example`。
