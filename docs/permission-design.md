# 权限关卡（S1）：CC 与 minicode 的两种放法，以及 CarryCode 的移植计划

> 状态：**v1.0 定稿（D1–D6 全部锁定）；阶段 0、1 已施工，阶段 2（事件流）未动** ｜ 2026-11-04
> 关联：[产品文档](../../产品文档.md) §5 事件流、§6 能力边界、§9.1 施工单第 5 步、§10 红线、§11 学习映射
> 参照物：[minicode/src/permissions.ts](../../minicode/src/permissions.ts)、[claude-code 工具接口笔记](../../minicode/external/MiniCode-Python/CLAUDE_CODE_ARCHITECTURE_LEARNING.md)

---

## 0. 一句话结论

两个参照物把审批放在两个不同层：**Claude Code 把"裁决"提到执行管道，工具只声明"我有多危险"；minicode 把"声明 + 裁决 + 提问"全部写进工具的 `run()` 里。**

CarryCode 该采用前者，但借用后者的"提前算好要给人看什么"。理由不是"CC 更先进"，而是产品文档已经承诺了三件事，而这三件事在 minicode 的结构里**没有位置可挂**：

| 文档已经承诺的 | 在 minicode 的结构里 | 在中央闸门的结构里 |
|---|---|---|
| `--readonly` / `--yes` 全局模式（§4.6） | 挂不上：工具不该知道全局模式，没人告诉它 | 一行查询 mode |
| `permission_request` 是**核心事件**（§5） | 挂不上：弹窗逻辑在 PermissionManager 内部，核心看不到 | 事件天然产生在闸门处 |
| 整回合可**无终端单测**（§8.3） | 半挂：`permissions?` 为 None 时退化成"全放行" | 注入假 `asker` 即可断言 |

所以这不是一个开放的品味问题：**要不要 readonly/auto 两种模式**这个决策在 §4.6 已经做了，它顺带把闸门钉在中央。

---

## 1. 两种放法

### 1.1 Claude Code：工具声明，管道裁决

```typescript
// 工具只回答"我这次调用有多危险"，不负责问人
type Tool = {
  name: string
  call(args, context, canUseTool, parentMessage, onProgress): Promise<ToolResult>
  isReadOnly(input): boolean          // 只读：走"自动允许"通道
  isDestructive?(input): boolean      // 破坏性：即使 auto 也可能要问
  checkPermissions(input, context): Promise<PermissionResult>   // allow / ask / deny + 原因
}
```

```
model tool_use
   ↓
loop ──→ 中央闸门 ── 规则(allow/deny/ask) + 模式 + hook + canUseTool 回调
          ↑
          └── 工具声明：checkPermissions / isReadOnly / isDestructive
   ├─ deny → tool_result(error) 回给模型（不是抛异常）
   └─ allow → tool.run()
```

外层的用户可配置面（设置文件里的 `permissions.allow/deny/ask`、`Bash(git diff:*)` 这类 specifier、`--permission-mode`、PreToolUse hook）都挂在"中央"这一侧，工具代码一个字不用改就能接受新策略。以官方文档为准：<https://code.claude.com/docs/en/settings-reference>。

### 1.2 minicode：写在工具里

```typescript
// minicode:src/tools/write-file.ts
async run(input, context) {
  const target = await resolveToolPath(context, input.path, 'write')        // → ensurePathAccess
  return applyReviewedFileChange(context, input.path, target, input.content) // → ensureEdit(diff)
}
```

```
model tool_use
   ↓
loop → ToolRegistry.execute → tool.run()          ← 管道里没有任何权限判断
                                 ├─ resolveToolPath()      → permissions.ensurePathAccess()
                                 ├─ applyReviewedFileChange → permissions.ensureEdit()
                                 └─ ensureCommand()         （bash 工具里）
                                 ✗ 没写这条路 = 直接跑
```

已确认的后果（上一轮读过源码）：

- `ToolRegistry.execute()` 只做 zod 校验 + `run()`，`ToolContext.permissions?` 是可选的；
- 动态注册的 MCP 工具 `run(input)` 连 `context` 都不接，完全在权限体系之外；
- 子 agent 调 `runAgentTurn` 时不传 `permissions`，`?.` 直接短路，靠工具白名单兜底。

---

## 2. 逐项对比

| 维度 | Claude Code | minicode |
|---|---|---|
| 危险性由谁判断 | 工具（`checkPermissions` / `isReadOnly`） | 工具（`ensureXxx` 里的逻辑） |
| 最终裁决由谁做 | 中央管道 + 规则 + 模式 + hook | 工具里的 `PermissionManager` |
| 新工具会不会自动被覆盖 | 会（默认要经过闸门） | **不会**（不写就不问） |
| 默认值方向 | 默认问 | 默认放行 |
| 全局模式 | `default` / `acceptEdits` / `plan` / `bypassPermissions` 等 | 没有模式，只有 allowlist + 会话/回合记忆 |
| 审批粒度 | 工具 + 规则 specifier（`Bash(npm run test:*)`） | 路径 / 命令签名 / 单个文件 |
| 策略谁改得动 | 用户（设置文件、CLI flag、hook） | 只有改代码 |
| 拒绝如何表达 | 三态结果 + 原因，变成 tool_result | 抛 `Error` → 被 `execute` 包成 `fatal: true` 的结果 |
| 审批 UI 的位置 | 交互层订阅判定结果 | `PermissionManager` 持有 prompt 回调 |
| 新增一个工具的成本 | 两个方法 + 元数据 | 一两行 `await` |
| 主要失败模式 | 过严：问太多、规则写不对就问不停 | 漏问：忘了写就静默放行 |

---

## 3. 各自的优势与代价

### 3.1 CC：声明 + 中央裁决

**优势**

1. **默认拒绝可强制**。闸门在唯一的执行入口上，新工具、第三方工具、MCP 工具自动被覆盖。工具作者只声明意图，决定权不在他手里。
2. **关注点分离得干净**：工具知道"这次调用有多危险"（它懂自己的入参语义——bash 要拆命令串、edit 要定位匹配），策略知道"现在该不该问"（模式、规则、用户偏好）。两者能独立演进。
3. **可配置**：加一条 `deny: Edit(**/.env)` 是改设置，不是改代码；切模式是改一个全局状态。
4. **可测试**：`checkPermissions` 是纯判定，能对每个工具单测判定结果，不需要跑真实副作用，也不需要终端。
5. **拒绝是数据不是事故**：拒绝作为一条带原因的 tool_result 回给模型，模型可以换路子；不会把整个回合打成 fatal。

**代价**

1. **抽象成本**：每个工具两个方法加若干元数据；对 6 个工具的学习项目是明显的固定开销。
2. **两处要同步**：判定逻辑理解入参的方式，必须和执行逻辑一致。判成 `read` 实际是 `write` 这类不一致，闸门看不出来。
3. **规则系统自身成为产品**：specifier 语法、优先级、错误处理、文档——这部分往往比闸门本体大好几倍。
4. **顺序依赖**：中央闸门要携带模式、规则、UI 回调，必须等循环骨架立起来才能接。

### 3.2 minicode：写在工具里

**优势**

1. **直白可读**：`await context.permissions?.ensureEdit(...)` 就写在动手前一行。"审批发生在哪"一眼可答——恰好就是 §11 那张表要你回答的问题。
2. **判断能用到最全的上下文**：它已经在 `run()` 里，拿着算好的 diff、解析好的真实路径、已经归一化过的参数来判断，不用把语义重新推导一遍。
3. **实现成本极低**：一个 `PermissionManager` + 每个工具一两行，没有 DSL、没有元数据。
4. **天然局部**：不需要中央知道每种工具的参数长什么样。

**代价**

1. **默认放行**：漏写一处就是一处无限制的入口，且没有任何地方会报错（MCP 工具、子 agent 都是这个坑的现实版本）。
2. **策略分散**：加一条策略要改 N 个工具；而"模式"根本无处安放——工具不该知道用户开了 `--readonly`。
3. **可选的依赖成为坑**：`permissions?: PermissionManager` 让"没有 manager"成为合法运行态，语义等价于"全放行"。子 agent 那条路已经踩上了。
4. **不好测**：要测"会不会问"，得跑 `run()` 的真实现（读文件、算 diff、起进程）。

---

## 4. 权衡：接缝该切在"风险"和"策略"之间

把两类知识分开看，结论就清楚了：

| 知识 | 谁有资格判断 | 例子 |
|---|---|---|
| **这次调用有多危险** | 工具 | bash 这条命令是不是写操作；edit 要改的是哪个文件的哪一段 |
| **现在该不该问** | 会话 / 用户 / 配置 | 现在是 `--readonly` 还是 `--yes`；用户刚才说了 `a`；测试里有假回答 |

- minicode：两半都塞进工具。
- CC：第一半留在工具（`checkPermissions` 返回判定素材），第二半在中央（规则 + 模式 + hook 合成最终裁决）。
- **CarryCode 应该照 CC 切**，但只取第一半的最小形态：工具**不返回判定**，只返回**事实**（风险级别 + 审批块预览行 + 受约束的路径参数）。判定全部在中央那一个函数里，因为判定要读的只有 mode、会话记忆、asker 三样东西。

这样做的直接收益，正好对着 §11 那道题：

> **"审批发生在工具的哪个位置？"**
> 答：不在工具里。在 `dispatch()` 调用 `run()` 之前；工具只声明"我有多危险"，不决定"要不要问"。

这个答案能讲清楚 CC 和 minicode 的分水岭，而照 minicode 抄下来的答案是"在工具里、动手前那行"——说得通，但推不出 readonly/auto 两种模式。

---

## 5. 落到 CarryCode：一条判定链

`dispatch(call, permissions)` 在 `spec["run"](args)` 之前按顺序走完，全程只有一个函数会问人：

```
0. 未知工具                                        → 拒（这是健壮性，不属于审批）
1. 取风险档：工具说了算（read / write / exec / irreversible）
2. 永久禁止：位置禁区（密钥目录、凭据文件）或行为禁区（`rm -rf`、`sudo`）→ 拒，不问
3. 写越界（PATHS 声明的路径不在工作区，且档位 != read） → 拒（硬边界，任何模式）
4. readonly 模式，且档位 != read                    → 拒
5. 必须问：不可逆档，或者读越界                      → 问，只给 y/N；记忆无效
6. auto 模式                                       → 允许
7. 没有人可以问（JSON / 管道 / CI）                  → 拒
8. 本会话记忆命中                                   → 允许 / 拒
9. 问：y 允许 ／ a 本会话允许该类型 ／ N 拒 ／ d 本会话拒该类型
10. 允许 → spec["run"](args)
```

整条链压成一句话：**策略只有六种干预，三种结论。**

| 干预 | 什么时候用 | 效果 |
|---|---|---|
| 位置禁止 | 写工作区外、读密钥目录 | 拒，不问，任何模式都不变 |
| 行为禁止 | `rm -rf`、`sudo` 这类"根本不打算做"的事 | 拒，不问，任何模式都不变 |
| 模式禁止 | `readonly` 下的写和执行 | 拒，不问 |
| 强制问 | 不可逆操作、读工作区外 | 问，只给 y/N；模式与记忆都不管用 |
| 常规问 | 工作区内的写和执行 | 按模式：`ask` 问 y/a/N/d，`auto` 直接放 |
| 无人可问 | JSON / 管道 / CI 里需要审批 | 拒 |

三种结论：**允许、拒绝、问**。其中"拒绝"有三个不同来源（位置不许、行为不许、要么模式不许要么人不许），**"不可逆"不属于拒绝，它属于问**。

**第 1 步必须在路径判断之前**，因为同一个"位置"对读和对写的结论是相反的：

| 情形 | 结论 |
|---|---|
| 读在工作区内 | 不问，直接读 |
| 读在工作区外 | 问（y/N），每次都要问 |
| 写 / 执行在工作区内 | 交给模式判断 |
| 写 / 执行在工作区外 | 拒（硬边界，任何模式都一样） |
| 读禁区（`~/.ssh` 这类） | 拒，不问；在工作区内也一样 |

四档风险，"要不要问"的四种不同答案：

| 档 | 谁定 | 典型 | 各模式下的行为 |
|---|---|---|---|
| `read` | 工具 | read_file / glob / grep | 不问；越界才问 |
| `write` | 工具 | write_file / edit_file | ask 问、auto 不问、readonly 拒 |
| `exec` | 工具 | bash 的默认档 | 同 write（只是审批块上的标签不同） |
| `irreversible` | **工具按这一次的入参算** | `git push`、`reset --hard`、`rm -rf` | **一律问**：auto 也问、记忆无效、只有 y/N |

几条硬规定：

- **`permissions` 是必填位置参数**，不给默认值。没有"忘了传就放行"这个状态。
- **拒绝走返回值，不走异常**：返回一条英文 tool_result，`is_error=True`，措辞告诉模型怎么绕（`denied: read-only mode; use read_file instead of bash`）。
- **写越界是边界，读越界是审批**：同一项检查，两种结论。写越界靠 `--allow-outside` 开，`--yes` 不放开；读越界每次问一次，`--yes` 也不跳过（`--yes` 跳的是"有副作用"那一层）。这样"模式管副作用、开关管写边界"两条线不交叉。
- **`RISK` 的默认值必须是"要问"**（缺省按 `write`，不是 `read`）。一个默认值就把 minicode 最贵的那个坑（漏写即放行）填上了：新工具不声明风险 = 会被问，而不是静默执行。
- **`irreversible` 这一档不允许被记住**。`a`（本会话不再问）对它无效，否则按一次 `a` 就把整档取消了。读越界的问句同样只给 y/N。
- **行为禁令必须提前写进系统提示**。`sudo`、递归删除这类只在执行时拦是不够的：模型不知道自己不该干，会反复试、换着法儿试，白烧 token。系统提示里那段话必须和 `bash.py` 的禁令表**同源**（由同一份常量生成），不许两处各写一份，否则改一处就漏一处。

### 5.1 工具的附加声明（三件套之外的可选件）

两种形状，共用一个契约：能写常量就写常量，需要按入参变就写一个函数。

```python
# 常见形状：固定档位 + 审批块内容
# tools/edit_file.py
NAME = "edit_file"
RISK = "write"
PATHS = ("path",)                               # 这个入参受工作区约束

def preview(args):                              # 审批块要给人看什么（只读，不许动文件）
    line, text = locate(args)                   # 从 run() 里抽出来的纯函数
    return [f"文件: {args.get('path')}", f"唯一匹配：第 {line} 行  {text}"]

# 少数形状：这一次到底多危险，要按入参算
# tools/bash.py
NAME = "bash"
RISK = "exec"

def inspect(args):
    """这一次到底多危险：返回 (档位, 审批块正文)。看不懂就退回 RISK 那一档。"""
    command = str(args.get("command") or "")
    verdict = commands.classify(command)     # 禁令 / 不可逆，见 5.2、5.3
    if verdict is not None:
        return verdict.level, verdict.lines
    return "exec", [f"命令: {command.strip()}"]
```

- 三个可选件各管一件事：`RISK` 管兜底档位，`preview()` 管"显示什么"，`inspect()` 管"这一次"的档位和显示什么。
- 同时有 `inspect()` 和 `preview()` 时，以 `inspect()` 为准（它连显示的行一起给了），`preview()` 不再调用。两个都没有就用 `RISK`（缺省 `write`）+ 默认两行（工具名 + 主要入参）。
- 回退方向只能往上：`inspect()` 给出的档位低于 `RISK` 时按报错处理，不按放行处理。
- 命名沿用现有习惯：常量大写（`NAME` / `SCHEMA` / `RISK` / `PATHS`），函数小写（`run` / `preview` / `inspect`）。

**为什么"有多危险"要留在工具里**：§4.3 的审批块要显示"唯一匹配：第 34 行 `def _grep(args, ctx):`"，这个位置只有 `edit_file` 算得出来；`git clean` 会删掉什么，也只有认得命令的那个工具知道。中央那一处不该懂 git 的语法和 edit 的匹配语义——它只负责把工具给的几行塞进 `permission_request`，然后按第 5 节那条链拍板。

**代价（要接受）**：`preview()` / `inspect()` 和 `run()` 各看一次文件或命令。规模上可以忽略，且 `run()` 本来就会重新校验（edit 的 0 处或多处、命令的空串），世界变了不会写出错误内容。不做缓存——缓存会把"预览"和"执行"耦合成一个状态机，不值得。

### 5.2 不可逆命令的问句规格（D2 的落地）

**一条本地表决定"什么算不可逆"，表里的文字就是问句的文字。**

三条铁律：

1. **事实由本地代码写，不由模型写。** "会失去什么""能不能恢复""影响范围"必须来自表里的固定文字。模型可以另附一句"我为什么要这么做"（意图），但不许由它定义危险程度——否则一个会说话的模型就能把危险说轻。
2. **命令原样显示。** 不翻译、不改写、不省略参数。长命令可以换行，但省略只能发生在完全无关的尾部（比如重复的路径列表）。
3. **表里没有的命令，不假装知道。** 显示"这条命令不在已知清单里，无法判断能否恢复"，并且**按普通风险处理**，不进不可逆档。把未知一律当不可逆等于每次都问，制度会立刻退化成人人无脑按 y。

问句骨架（六行，每行一句话，放在 §4.3 样张里审批块的位置）：

```
要执行一条不可撤销的命令，允许吗？
  命令：git reset --hard origin/main
  会失去：工作区里所有未提交的改动，包括你手改过但还没提交的部分
  能不能恢复：不能。git 不会为未提交的改动留副本
  影响范围：只影响这一个仓库，远端不受影响
  更安全的做法：先 git stash，改完再 stash pop
  选项：y = 就这样执行（不可撤销）  N = 不执行
```

初始清单（阶段 1 照这张表写死，行数固定，agent 不能扩充）：

| 命令形状 | 会失去 | 能否恢复 |
|---|---|---|
| `git push` | 提交离开发出，可能立刻被别人拿到 | 能，但要别人配合，CI 可能已经跑过 |
| `git push --force` / `-f` | 远端的历史 | 基本不能 |
| `git reset --hard` | 未提交的改动，以及分支指针的位置 | 文件内容不能；提交位置还能在 reflog 里找 |
| `git clean -f` / `-d` / `-x` | 没被跟踪的文件（含被 `.gitignore` 的） | 不能 |
| `git checkout -- <路径>` / `git restore` | 指定文件的未提交改动 | 不能 |
| `npm publish` | 一个版本号 | 版本号基本被永久占用 |
| `pip uninstall` / `npm uninstall -g` | 本机装好的包 | 能重装，但配置可能一起丢 |

**删目录类和提权类不进这张表**：`rm -rf`、`del /s /q`、`sudo` 属于第 2 步的**行为禁区**——不是"问一次还允许做"的事，而是**完全拒绝**。这张表只装"用户可能真的要做、所以值得问一次"的命令。

（`git clean -fd` 这种介于两者之间的，归哪边是**待穷举**的细节，先留在表里。）

配套两条：

- **选项只有 `y` / `N`**，不给 `a`。给 `a` 等于把这一档取消掉，自相矛盾。
- **`--yes` 模式下这一档仍然问**。这就是 D2 的全部内容：`--yes` 跳过的是常规审批，不是这一层。

### 5.3 行为禁令：那些"根本不打算做"的事

判据三条，都满足才进这张表；否则进 5.2 的不可逆表：

1. 一条命令就能造成不可挽回的损失；
2. 在"改代码"这件事里没有正当用途；
3. 正常用法本身就是破坏性的。

| 类 | 命令形状 |
|---|---|
| 提权 | `sudo`、`runas`、`gsudo` |
| 递归删除 | `rm -r` / `-R` / `-rf`、`del /s`、`rd /s` |
| 磁盘级 | `format`、`diskpart`、`mkfs`、`dd` |
| 关机重启 | `shutdown`、`reboot` |
| 注册表 | `reg delete` |
| 改权限 | `takeown`、`icacls` |
| 网络直执行 | `curl ... \| sh`、`iwr ... \| iex` |

已经拍过的三个边界：

- `git push --force`、`git clean -fd` → **不可逆**（问一次），不是禁令：它们有正当用途。
- `curl ... | sh` → **禁令**：内容无法审查。代价是"装个工具"类任务会卡住，让它改用包管理器。

匹配方式（已定）：

1. 按 `&&`、`||`、`;`、`|`、换行把命令串切成段；
2. 只看每段开头的命令名与子命令，**不做全文搜词**；
3. 理由很具体：`git commit -m "fix rm -rf in docs"` 不能误伤，而这段字符串在这个项目里天天出现。

**这张表防手滑，不防蓄意绕。** `python -c "import shutil; shutil.rmtree('.')"`、`powershell -c "Remove-Item -Recurse"`、把命令写进脚本再执行——都抓不到。要真拦，需要操作系统级沙箱，不是一张表。这句话要同时写进产品文档，免得以后有人把它当安全保证。

### 5.4 禁区：这几类内容永远不读

不论在不在工作区内、任何模式、**连问都不问**：

- `~/.ssh`、`~/.aws`、`~/.gnupg`、`~/.config/gcloud`、`~/.git-credentials`、`~/.netrc`
- 后缀为 `*.pem`、`*.key`、`*.pfx`、`*.p12` 的文件
- 工作区里的 `.env` 与 `.env.*`（**例外**：`.env.example` / `.env.sample` / `.env.template` 可以读）

两条精确化：

- **拦的是"读内容"，不是"看到名字"。** `glob` 照常能列出 `.env` 存在（确认配置有没有配是有用的），`read_file` / `grep` / `type` 拿不到内容。
- **bash 侧也要执行**，否则 `type .env` 一句话就绕过去了。具体粒度见 D5。

---

## 6. 移植计划（四阶段，每阶段独立可验收）

现状基线：805 行 Python、6 个工具、`dispatch()` 无闸门、`__main__.py` 里既有循环也有 print。

### 阶段 0 · 账本（不改行为）· ✅ 已完成（2026-11-04）

实际改动：

- 6 个工具各写一行 `RISK`：`bash=exec`、`write_file`/`edit_file=write`、`read_file`/`glob`/`grep=read`；
- 4 个接路径的工具写 `PATHS = ("path",)`；`glob`（只吃 `pattern`、从 cwd 出发）和 `bash`（只吃命令串）不声明；
- `edit_file` 把"定位 + 计数"从 `run()` 抽成 `locate(text, old) -> (行号, 次数)`，`run()` 与 `preview()` 都调它；
- `write_file` / `edit_file` 各写一个 `preview()`，只读（`write_file` 用 `exists()` 区分"新建"和"整份覆盖"）；
- `register()` 收 `risk` / `paths` / `preview` 三个关键字参数，缺省 `risk="write"`（落在"要问"那一边）；`tools/__init__.py` 用 `getattr` 取，装配仍是唯一知道具体工具的地方；
- `tests/test_architecture.py` 新增 `SecurityLedgerTest` 四条：账本断言、risk 取值合法、"每个工具必须显式写 RISK"、"preview 里不许出现写操作"。

验收：`python -m unittest discover -s tests -t .` → 8 条全绿；`dispatch` 行为未变（它还没读这三个新字段）。

实测账本：

```
bash         exec         ()
read_file    read         ('path',)
write_file   write        ('path',)   preview
edit_file    write        ('path',)   preview
glob         read         ()
grep         read         ('path',)
```

**留给阶段 1**：`bash` 的禁令表、不可逆表、`inspect()`；`permissions.py` 与 `dispatch` 的闸门。

### 阶段 1 · 闸门（无终端可测）· ✅ 已完成（2026-11-04）

实际改动：

- 新增 `carrycode/permissions.py`：`Request` / `Decision` / `Permissions(mode, asker, emit, workspace, allow_outside)`；`check(tool, args, spec) -> Decision` 按第 5 节那条链走；不 print、不 input、不 import 渲染代码。
- 新增 `carrycode/tools/commands.py`：命令串的知识（切词、找命令名、禁令表、不可逆表、禁区名字匹配）。单独成文件，纯函数可以直接单测。
- `tools/tool.py`：`dispatch(call, permissions)`，`permissions` 是必填位置参数；拒绝走返回值。
- `tools/base.py`：禁区表（`.env*`、`id_rsa`、`*.pem`、`~/.ssh`…）+ `secret_name_reason` / `secret_path_reason` / `is_within`。
- `tools/bash.py`：`inspect()` 接 `commands.classify()`。
- `tools/grep.py`：遍历时跳过禁区文件——否则 `grep -r API_KEY .` 会把 `.env` 的行读出来。
- `__main__.py`：`_ask_permission()` 渲染审批块并读一个答案；加 `--yes` / `--readonly` / `--allow-outside`；调用改成 `dispatch(call, permissions)`。

验收：`python -m unittest discover -s tests -t .` → **35 条全绿**（8 条架构 + 27 条审批）。

实测（全部答 `n`，不真的执行任何东西）：

```text
> bash git push
要执行一条不可撤销的命令，允许吗？
  命令: git push
  会失去：这次提交留在本机的独占权
  能不能恢复：能，但要别人配合；CI 可能已经跑过
  影响范围：远端仓库，不只是本机
  更安全的做法：先 git log 看一眼要推什么
  [y/N]

> bash "sudo rm -rf /"                     → 拒，不问
> bash "type .env"                         → 拒，不问
> read_file ".env"                         → 拒，不问
> bash 'git commit -m "docs: explain .env"' → 不是禁区，走 [y/a/N/d]
> read_file "README.md"                    → 不问，直接读
--yes 下 git push                          → 仍然问，[y/N]
--readonly 下 write_file                    → 拒，不问
```

两处与设计稿的有意收窄：

1. **没做只读命令白名单**：`dir`、`git status` 也走常规审批。白名单永远不全，先不给自己一个"看着安全"的错觉；阶段 3 再谈。
2. **`--allow-outside` 一开，读越界也不再问**：否则这个开关只开了一半，行为不好解释。

### 阶段 2 · 事件与入口

依赖施工单第 1 步（`events.py`）已到位。

- `permission_request` 事件在闸门里 `emit`（载荷：工具名、入参摘要、风险级别、`preview()` 的几行）；
- 系统提示加一段"这几类不做"（提权、递归删除），文字由 `bash.py` 的禁令表生成，**不手写第二份**；
- CLI 订阅者渲染 §4.3 的审批块并读 `[y/N/a]`，作为 `asker` 注入；
- `--readonly` / `--yes` / `--output json` 三个入口共用同一核心，只有订阅者不同；
- `--output json` 下的 `asker` 必须是"缺省拒绝"，否则 JSON 模式会卡在 `input()`。

一个临时妥协要写清：如果先做闸门、`events.py` 还没到位，`emit` 可以先传 `None`，**但临时的 `print` 只许留在 `cli.py`，不许进 `permissions.py`**——否则"核心不 print"这条线第一次就破了。

验收：场景 B 在三种模式下跑通——默认模式三次审批块、`--readonly` 在 `edit_file` 处被拒且模型改用只读方式、`--yes` 零审批；`--output json` 下不卡住。

### 阶段 3 · 收紧（延后，按 §10 补）

- `--allow-outside` 开关的实现（阶段 1 已留判定点，这里只加开关）；D3a 若选 B，则 `PATHS` 只在"写"方向生效；
- 不可逆表的扩充（只加能被一条命令做完、且损失不可逆的形状；宁缺毋滥）；
- 会话级 allowlist 落盘（minicode 的 `permissions.json`）→ **S1 不做**，见第 9 节。

---

## 7. 决策记录

### 7.1 已锁定

| # | 决策 | 结论 | 日期 | 理由 |
|---|---|---|---|---|
| D2 | `--yes` 下遇到不可逆命令 | ✅ **仍然问一次**；选项只有 `y` / `N`；不受"本会话不再问"影响 | 2026-11-04 | §2 原本就写着"不自动执行不可逆操作"，`--yes` 跳过的是常规审批，不是这一层 |
| D2a | 问句要多详细 | ✅ **六行骨架，字段固定**（命令 / 会失去 / 能否恢复 / 影响范围 / 更安全的做法 / 选项）；**事实由本地模板写，模型不许写** | 2026-11-04 | 要"尽可能详细说明"，同时不能让会说话的模型把自己的危险说轻 |
| D3 | 出工作区 | ✅ **写越界是硬边界**（任何模式都拒，只认 `--allow-outside`）；**读越界走审批**（每次都问，只给 y/N） | 2026-11-04 | 写会留痕、读不会；但读也不是没代价的——内容会进模型上下文，工作区外可能有密钥。所以不放开，也不封死 |
| D3a | 读的禁区 | ✅ 与 D3 合并：读越界可以问，但不给 `a`；`~/.ssh`、`~/.aws`、凭据文件这类**连问都不问，直接拒**（在工作区内也一样） | 2026-11-04 | 禁区不是"位置问题"，是"读到就出事" |
| D4 | `a` / `d` 的粒度 | ✅ **B：write/edit 按工具名；bash 按命令的第一个词**（`git` / `npm` / `python`） | 2026-11-04 | A 等于把 shell 放开；C 太细记不住。审批块上要把 `a` 的后果写全（"本会话所有 git 命令不再问"） |
| D6 | `.env` 原件 | ✅ **进禁区，永远不读**；`.env.example` / `.env.sample` / `.env.template` 可读；`.env.*` 一律禁 | 2026-11-04 | 里面是 API key，读进上下文等于把 key 发出去。只读工具要拦，bash 侧也要拦（粒度见 D5） |
| D1 | 决定放在哪 | ✅ **A：统一放在"调用工具的那一刻、工具动手之前"** | 2026-11-04 | 放进工具里就挂不上 readonly/auto，也回到"忘了写就是漏洞" |
| D5 | bash 侧怎么执行禁区 | ✅ **B：命令切成词，词完全等于禁区名（或以 `\` `/` 结尾于它）才拒** | 2026-11-04 | `type .env` 命中；`git commit -m "docs: explain .env"` 不命中；整串子串匹配会误伤 |

### 7.2 待定

无。D1–D6 全部锁定，按第 6 节施工即可。

不必单独拍板、按文档走即可的一条：`a`（本会话该工具不再问）按**工具名**记忆（§4.5 原文），不是按命令前缀，也不是按文件——后者是 S2+ 的事。

---

## 8. 验收清单（对齐 §8.3 与 §11）

- [ ] 单测总数 ≥20（新增 ≥10 条审批测试），全绿；
- [ ] 三场景：A 零审批、B 三次审批、C 至少一次审批且修好后复跑；
- [ ] 整回合能在**无终端**下用假 `asker` 跑完并断言事件序列；
- [ ] `permissions.py` ≤200 行、`tool.py` ≤90 行、总量仍在 ≤1500；
- [ ] 能口述："审批发生在工具的哪个位置？" → 在 `dispatch()` 里、`run()` 之前；工具只声明风险；
- [ ] 能口述："`--readonly` 为什么放不进工具里？" → 因为那是全局模式，工具不该知道；
- [ ] `grep -I "sk-"` 仍然干净（无密钥泄漏）。

---

## 9. 明确不做（S1 边界）

| 不做 | 理由 |
|---|---|
| 规则 DSL（`Edit(**/.env)`、`Bash(git diff:*)`） | 这是"策略系统"，比闸门大好几倍；S1 只有 6 个工具，工具粒度的 `a` 足够 |
| allowlist 落盘到 `~/.carrycode/permissions.json` | 跨会话策略属于"用户偏好"，S2 之后再谈；先保证单会话行为正确 |
| MCP / 子 agent 的权限继承 | 产品文档 Non-goals 里没有这两项；等它们真加进来时，中央闸门天然覆盖（这正是选 A 的期权价值） |
| 审批内容翻译成中文（`idea.md` 第 1 项） | 是审批块**文案**问题，不是闸门问题；等闸门与事件都到位后单独做 |
| diff 全文进审批块 | §4.3 样张只要求"关键入参 + 匹配位置"；全文 diff 属于 TUI 阶段 |
| 解析 `bash` 命令串里的路径做工作区约束 | 不解析"这条路径在不在工作区内"——那需要命令解释器。只做两件粗的：切段看命令名（5.3）、禁区的词匹配（5.4 / D5）。老实话：`bash` 的安全靠审批，不靠解析；`auto` 模式下 `bash` 基本等于放开 |

---

## 10. 与 minicode 的一处刻意分歧（留档）

minicode 在非 TTY 下对需要审批的操作**抛异常**（`Path ... is outside cwd ... Start minicode in TTY mode to approve it`），异常被 `ToolRegistry.execute` 包成 `fatal: true` 的结果。

CarryCode 改为**返回一条普通拒绝结果**（`is_error=True`，不带 fatal 语义），理由：

1. 模型看到"这条路被封了、请换只读方式"，比看到"工具崩了"更可能自我纠正；
2. 一条 `deny` 在事件流里是可见的（§4.2 原则 4"失败不装死"），而 fatal 会把回合推向停止；
3. 测试里断言"run 没被调用"比断言"抛了异常"更贴近真实语义。

---

## 11. 附录：现行 harness 的实测（D3 / D3a 的依据）

2026-11-04 在本机做了四步探测，结果：

| 探测 | 结果 |
|---|---|
| 读工作区外的文件（`C:\Windows\win.ini`） | **成功** |
| 写工作区外（`D:\CST\code\_probe.txt`） | **被拒**。拿到的是操作系统的"访问被拒绝"，全程没有任何弹窗，也没有审批机会 |
| 写平台临时目录 | 成功（这是刻意的例外） |
| 写工作区内 | 成功 |

结论：这个 harness 的边界是**按写入画的硬墙**，不是一串审批提问。读是开放的，写在墙内。

还有一条设计上的佐证：它的命令工具在越界被拒之后，允许"带着更宽的权限、重试刚刚那一条命令"，而那次重试才会问用户。也就是说，顺序是 **先硬拒 → 再显式申请 → 申请时才问**，不是"一开始就问"。

对 CarryCode 的两点启示：

1. **"边界"和"审批"确实该分成两条线。** 硬边界不需要、也不该有 `[y/N/a]`；它只需要一个显式的开关。D3 选 A 是对的。
2. **读的边界，我们比 harness 严一档。** harness 把读完全放开；我们决定"读越界要问一次、禁区直接拒"。理由：读不改文件，但读会把内容送进模型上下文——工作区外读到的东西里可能是密钥。这条在 `permissions.py` 里写死，不开放配置。
