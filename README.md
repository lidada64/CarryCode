# CarryCode

一个**只用 Python 标准库**的最小 agent loop：模型可以调用 11 个工具（`bash` / `read_file` / `write_file` / `edit_file` / `glob` / `grep` / `update_plan`，再加 4 个管理只读子 agent 的工具），看结果，再决定下一步；需要同时查几件事时，它还能派只读子 agent 出去。

没有配置分层、没有落盘的会话——历史和上下文只在内存里，计划、子 agent 记录也一样。入口在 `carrycode/__main__.py`，请求在 `carrycode/model.py`，循环在 `carrycode/agent_loop.py`，子 agent 在 `carrycode/subagents.py`，工具都在 `carrycode/tools/` 下（一个工具一个文件），审批在 `carrycode/permissions.py`。

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
| `/plan` | 看当前计划（只读，不打印到聊天记录里） |
| `/new` | 清空历史和计划、关掉还在跑的子 agent，开一段新对话 |
| `/exit`（或 Ctrl-D） | 退出 |
| Ctrl-C | 中断当前这一轮，历史回到中断前（计划不回退） |

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

1. 把任务、系统提示、11 个工具的定义发给模型；
2. 模型返回 `tool_use` 就交给 `dispatch()`：查名字、跑函数、把结果变成 `tool_result` 回传；
3. 重复直到 `stop_reason` 不是 `tool_use`，打印最终回答；最多 8 步（收尾那次综合请求不算在内）。

assistant 的原始 content blocks（含 thinking）原样进历史——DeepSeek 的 Anthropic 兼容端点要求这样回传。

### 计划

三个组件各管一件事，谁都不兼职：

| 组件 | 文件 | 职责 |
|---|---|---|
| `PlanManager` | `carrycode/plan.py` | 存清单、校验、通知订阅者 |
| `update_plan` | `carrycode/tools/update_plan.py` | 模型改清单的唯一入口 |
| `with_plan_context` | `carrycode/plan_context.py` | 把最新清单拼进这一次请求的 system |

模型每次提交**完整清单**：已有条目带上原来的 `id`，新条目不带 `id`；同一时间最多一个 `in_progress`；空数组表示清空。不认识的 `id`、缺 `title`、`status` 写错都会被拒，而且拒的时候内部状态一个字段都不动。

清单**不塞进 messages**：每次调用模型前重新读一份快照拼到 system 末尾。所以历史保持只追加（前缀缓存还能用），压缩掉多少也不影响清单，会话日志里也不会越积越多旧副本。

计划只活在内存里：`/new` 一起清空；以后加 `--resume` 时只恢复对话，不恢复计划。没有计划时，一条用户提示进来的第一份请求会带一句"当前没有计划。多步骤任务可使用 update_plan；简单任务直接执行。"，之后同一轮不再重复。

界面要看进度就读 `PlanManager` 的快照（`/plan` 就是这么做的），不要去聊天记录里解析。

### 子 agent

模型可以把一件独立的调查甩给只读子 agent：`spawn_agent` 立刻返回 `agent_id`，worker 在自己的线程里跑同一个循环（`agent_loop.py`），但有自己的消息、system 和工具清单——看不到主对话，也碰不到计划。

| 工具 | 干什么 |
|---|---|
| `spawn_agent` | 起一个 worker，立刻返回 `agent_id` 和 `running` |
| `list_agents` | 本会话创建过的 worker：id、任务、状态，失败的带错误摘要 |
| `wait_agent` | 等一批 id（默认 30 秒，最长 120 秒）；完成带报告，失败带错误，超时不关它们 |
| `close_agent` | 请求关闭，等线程真的退出后返回最终状态 |

```
→ spawn_agent: {"task": "数一下 tests 目录里有哪些测试文件"}
→ 启动子 agent agent_1：数一下 tests 目录里有哪些测试文件
← agent_1 完成
← ok · 44 字符
```

规矩：

- 同时最多 3 个 worker，一个 worker 最多 20 次模型请求；schema 和执行层都只给它 `read_file` / `glob` / `grep`，模型伪造 `bash` 也进不了 `dispatch`。
- 主、子 agent 共用**一把请求锁**（`model.py`）：一次只发一份请求，响应读完才放锁——锁只包请求，不包工具。
- 终端上的启动/结束提示是整行加锁输出的（`_line()`），不会和 markdown 渲染插在一起；worker 的中间过程不打印。
- 一回合结束时还有没交付的报告，主 agent 先等 worker 全停，把报告注入上下文再请求**一次综合**（`synthesize`，不带工具），然后才给最终答复；在那之前模型准备收尾的那段文本先压着不发。
- 综合没成功（请求失败、没有有效文本、回复里还想调工具、被 `max_tokens` 截断）就把原始报告摆出来，并明说这一回合没完成——不假装综合过。没配对的 `tool_use` 不会进历史。
- 报告按「哪个主回合创建」交付：`wait_agent` / `close_agent` 交过的不会再注入一遍；`list_agents` 仍然列出本会话全部 worker。Ctrl-C 会回滚这一轮历史，并给这一轮的 worker 发关闭请求（只请求，不等）。
- worker 的审批是另一份 `Permissions`（`asker=None`）：读工作区不用问，需要问的（比如读工作区外面）一律拒——后台线程不该抢终端的输入。
- `/new` 和退出都会先请求关闭、再等 worker 真的退出；已经发出去的同步请求没法强杀，可能要等它返回或超时。

子 agent 的记录只在内存里，退出就没了；worker 不能派孙 agent，也不能改代码——改代码始终只有主 agent 能做。

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
| 模型请求（含子 agent） | 7 |

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

模型请求和工具调用都把子 agent 的算进去（同一把锁、同一份账），任务轮数只数主 agent 的回合。

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
| `update_plan` | 整张换掉当前会话的待办清单 | `todos`（每项 `title`、`status`，已有项再带 `id`） |
| `spawn_agent` | 派一个只读子 agent 查一件独立的事，立刻返回 id | `task` |
| `list_agents` | 列出本会话的子 agent：id、任务、状态、错误摘要 | 无 |
| `wait_agent` | 等一批子 agent 结束（默认 30 秒，最长 120 秒） | `agent_ids`、`timeout_ms` |
| `close_agent` | 请求关闭并等它真的退出 | `agent_id` |

`edit_file` 的 `old_string` 必须和文件里的字符完全一致（含缩进和换行）；找不到、或者出现多次，都直接作为错误结果返回，让模型自己补上下文——不去猜改哪一处。CRLF 的文件会自动保住原来的行尾。

`glob` 和 `grep` 默认跳过 `.git`、`node_modules`、`__pycache__`、`dist` 这类目录；`grep` 还会跳过二进制文件和超过 2MB 的文件。

## 现在没有

- 会话持久化（历史只在内存里，退出就没了）；计划同理，新会话从空清单开始。
- 子 agent 的记录和报告也只活在内存里，退出就没了。
- 流式输出 / 大结果外置到文件。
- Unix 语义：Windows 上命令走 `cmd`，`ls` 会失败，用 `dir` 或 `python`。

## 跑测试

```powershell
python -m unittest discover -s tests -t .
```

现在有十组：架构约束（工具层不许反向 import、工具顺序固定、每个工具都有说明和参数、空参数工具要登记）、审批判定（假 asker，断言问没问、允不允许）、计划状态（整表替换、id 规则、订阅、重置）、计划的注入（空计划只提一句、有计划就带最新清单）、循环里的计划（假模型，断言每份请求带的是最新清单、空计划只催一次、注入不进历史）、`update_plan`（接参、错误结果、未绑定时的兜底）、子 agent（独立消息、共享请求锁、白名单、并发上限、等待/超时、关闭、自动收集与失败兜底、Ctrl-C 和 `/new` 清理、终端提示）、子 agent 工具（参数校验、未绑定兜底、JSON 结果）、会话报告与钩子（不联网，断言统计和 markdown 拼得对）、渲染（纯文本兜底的结构与"不出 ANSI"）。

## 文件

```
CarryCode\
├─ carrycode\
│  ├─ __init__.py          版本号
│  ├─ __main__.py          入口：装配会话、主回合与收尾、对话框、终端输出
│  ├─ model.py             一次模型请求：共享锁、超时、错误转换、load_config
│  ├─ agent_loop.py        模型 → 工具 → 模型的循环 + 收尾综合（主、子共用）
│  ├─ subagents.py         子 agent：记录、线程、等待/关闭、报告拼装
│  ├─ plan.py              计划状态：PlanManager（存、校验、订阅）
│  ├─ plan_context.py      把最新清单拼进 system（with_plan_context）
│  ├─ permissions.py       审批：三档模式 + 边界 + 记忆，十步判定链（不 print、不 input）
│  ├─ hooks.py             生命周期钩子：session_start / session_end，出错不影响主流程
│  ├─ render.py            回答的 markdown 渲染：rich 可用就排版，否则纯文本兜底
│  ├─ session_report.py    会话统计 → markdown → 交给 render.py 渲染
│  └─ tools\
│     ├─ __init__.py       装配：装哪几个工具、按什么顺序（唯一能改顺序的地方）
│     ├─ tool.py           花名册 + dispatch（不认识任何具体工具）
│     ├─ base.py           公共件：路径、文本读写、参数兜底、禁区表（不 import 任何东西）
│     ├─ agents.py         四个管理工具共用的接线：bind / current（和 update_plan 一个路子）
│     ├─ commands.py       命令串的知识：切词、找命令名、禁令表、不可逆表
│     ├─ bash.py           每个工具一个文件，里面是 NAME / SCHEMA / run 三件套
│     ├─ read_file.py      外加可选的 RISK / PATHS / preview / inspect
│     ├─ write_file.py
│     ├─ edit_file.py
│     ├─ glob.py
│     ├─ grep.py
│     ├─ update_plan.py
│     ├─ spawn_agent.py
│     ├─ list_agents.py
│     ├─ wait_agent.py
│     └─ close_agent.py
├─ tests\
│  ├─ __init__.py
│  ├─ test_architecture.py 架构约束：不许反向 import、顺序固定、安全账本
│  ├─ test_permissions.py  审批：全程无终端，用假 asker 断言"问没问、允不允许"
│  ├─ test_plan.py         计划状态：整表替换、id 规则、订阅、重置
│  ├─ test_plan_context.py 注入：空计划只提一句、有计划就带最新清单
│  ├─ test_plan_loop.py    循环与对话框：假模型，断言每份请求的 system
│  ├─ test_plan_tool.py    update_plan：接参、错误结果、未绑定时的兜底
│  ├─ test_subagents.py    子 agent：独立消息、共享锁、白名单、收尾、Ctrl-C 与 /new
│  ├─ test_subagent_tools.py 四个管理工具：参数校验、未绑定兜底、JSON 结果
│  ├─ test_render.py       渲染：纯文本兜底的结构与"不出 ANSI"
│  └─ test_session_report.py 会话报告与钩子：不联网，只断言拼出来的东西对不对
├─ docs\permission-design.md  审批的设计与决策记录（D1–D6）
├─ .env.example            密钥模板
├─ run-carrycode.cmd       一键启动，可在任意目录运行
└─ pyproject.toml          包元数据
```

加一个工具 = 新建 `tools/<名字>.py`（`NAME` / `SCHEMA` / `run` 三件套 + 一行 `RISK`）+ 在 `tools/__init__.py` 的 `_MODULES` 末尾加一行，其它文件一个字不用动。

忘了写 `RISK` 会被测试抓住；而且 `register()` 的缺省值落在"要问"那一边，不会静默放行。

需要共享状态的工具（比如 `update_plan` 和四个子 agent 管理工具）走的是另一条路：工具层不许向上 import，所以状态由会话在启动时 `bind()` 进去，工具只负责接参和转字符串。四个管理工具共用 `tools/agents.py` 里那一个引用。

## 已知限制

- 命令超时 120 秒，输出回给模型前截断到 12000 字符。
- Windows 上超时只杀直接子进程，孙进程可能残留。
- 模型可能不按约定调用工具；失败会作为工具结果回传，让它自己调整。
- 子 agent 只读、不能嵌套；它们需要审批的调用（比如读工作区外面）一律拒——后台线程没有终端可问。
- `close_agent`、`/new` 和退出都要等 worker 真的停下；已经发出去的模型请求没法强杀，最多等它超时。
- 综合请求只有一次：失败就把原始报告摆出来并标明没完成，不会自动重试。

## 密钥红线

本仓库是公开的：`.env` 被 `.gitignore` 排除，永不入库；模板是 `.env.example`。
