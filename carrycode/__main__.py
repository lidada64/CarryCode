"""CarryCode 最小 agent loop（纯标准库）。

一轮任务：

    用户 → 模型 → stop_reason=tool_use → dispatch() 执行工具 → 结果回传 → 模型继续
    → 直到 stop_reason 不是 tool_use；这中间模型还可以派只读子 agent 去查事情

用法：

    .\\run-carrycode.cmd                      # 进对话框，一行一条任务
    python -m carrycode "看看这个目录"        # 一次性跑完就退出

    对话框里：/help 看命令，/new 清空历史，/exit 或 Ctrl-D 退出，Ctrl-C 中断当前轮

配置：先读仓库根的 .env，再用同名环境变量覆盖

    ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL / ANTHROPIC_MODEL

文件分工：

    __main__.py        入口：装配会话、主回合与收尾、对话框、终端输出
    model.py           一次请求：锁、超时、错误转换（load_config 也在这里）
    agent_loop.py      模型 → 工具 → 模型的循环（主 agent 和子 agent 共用）
    subagents.py       子 agent：记录、线程、等待、关闭、报告拼装
    plan.py            计划状态：存和校验（PlanManager）
    plan_context.py    把最新清单拼进 system（with_plan_context）
    tools/base.py      公共件：路径、文本读写、参数兜底、共享常量（不 import 任何东西）
    tools/tool.py      花名册 + dispatch（不认识任何具体工具）
    tools/__init__.py  装配：装哪几个工具、按什么顺序（唯一能改顺序的地方）
    tools/<name>.py    单个工具：三件套 NAME / SCHEMA / run

工具清单在模块加载时装一次，之后不再改。这样每次请求发出去的工具说明都一样，
模型服务端的前缀缓存才认得出来；要改顺序或改说明，等于让前缀失效，得当成一次
有代价的变更来做。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from . import __version__, agent_loop
from .hooks import SESSION_END, SESSION_START, Hooks
from .model import Model, load_config
from .permissions import Permissions
from .plan import PlanManager, format_plan
from .plan_context import with_plan_context
from .render import render_markdown, rich_available
from .session_report import SessionStats, make_session_report_hook
from .subagents import MAX_CONCURRENT, SubAgentManager, format_reports
from .tools import TOOL_SCHEMAS, bind_agents, dispatch, update_plan

MAX_STEPS = 8  # 一回合最多 8 次模型请求：stop_reason 不是 tool_use 就停


# ---------------------------------------------------------------------------
# 终端输出：主线程和子 agent 线程都从这里走，行与行之间不会互相插进去
# ---------------------------------------------------------------------------

_OUTPUT_LOCK = threading.Lock()


def _line(text: str = "", file: TextIO | None = None) -> None:
    """整行一次写出去。子 agent 在自己的线程里也调它，所以加锁。"""
    stream = file or sys.stdout
    with _OUTPUT_LOCK:
        stream.write(text + "\n")
        stream.flush()


def _render(text: str) -> None:
    """渲染一段 markdown。和 _line 用同一把锁，状态行不会插进排版中间。"""
    with _OUTPUT_LOCK:
        render_markdown(text)


# ---------------------------------------------------------------------------
# 系统提示
# ---------------------------------------------------------------------------


def system_prompt() -> str:
    shell = "cmd" if sys.platform == "win32" else "sh"
    return f"""你是 CarryCode，一个跑在用户本机终端里的编程助手。

当前环境：
- 工作区：{Path.cwd()}
- 系统：{sys.platform}（命令通过 {shell} 执行）

规则：
1. 与用户交流用中文；命令、路径、代码、文件内容保持原样，不要翻译。
2. 看文件用 read_file（带行号，一次没读完就用 offset 接着读），按名字找文件用 glob，
   按内容搜用 grep；不要用 bash 的 type / cat / dir / findstr 代替它们。
3. 改文件用 edit_file：把一段唯一出现的原文替换掉；只有整份重写才用 write_file。
4. 跑测试、git、安装依赖和其它命令用 bash，一次一条命令。
5. 工具结果里 is_error=true 表示这一步失败了，读内容再决定下一步；bash 看 exit_code。
6. 只有真的做完才给结论；没有执行过的步骤不要说成已完成。
7. 不要执行破坏性命令（删库、格式化、git push），除非用户明确要求。
8. 需要同时查几件不相干的事、或者一段排查可以甩出去时，用 spawn_agent 派只读子 agent：
   它有自己的上下文、看不到这轮对话，只能用 read_file / glob / grep，最多 {MAX_CONCURRENT} 个同时跑。
   任务要写清楚查什么、要什么证据；改代码、跑命令仍然只能由你来做。
9. 用 wait_agent 收子 agent 的报告（一次可以等多个），跑偏了用 close_agent 停掉，
   list_agents 看现在都有谁、什么状态。
10. 一回合结束时还在跑的子 agent 会被自动等完，报告交回给你写最终答复；
    报告没到之前，别把它该查的结论当成已经查清。
"""


# ---------------------------------------------------------------------------
# 一场会话：主 agent 和子 agent 共用这些对象
# ---------------------------------------------------------------------------


@dataclass
class Session:
    """入口装配好之后，主循环和子 agent 管理器拿的是同一份东西。"""

    config: dict[str, str]
    model: Model
    permissions: Permissions
    plan: PlanManager
    subagents: SubAgentManager
    stats: SessionStats

    def request(
        self,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """主 agent 的一次请求：顺手记账。子 agent 在自己的线程里记自己的。"""
        self.stats.record_request()
        data = self.model.call(messages, system, tools)
        self.stats.record_usage(data.get("usage"))
        return data


# ---------------------------------------------------------------------------
# 给人看的零碎：调用说明、审批、提示符、抬头
# ---------------------------------------------------------------------------


def _describe_call(call: dict[str, Any]) -> str:
    """打印一行给人看的调用说明。"""
    name = str(call.get("name") or "")
    args = call.get("input") or {}
    if name == "bash":
        return f"bash: {args.get('command', '')}"
    detail = json.dumps(args, ensure_ascii=False)
    if len(detail) > 120:
        detail = detail[:120] + "…"
    return f"{name}: {detail}"


def _ask_permission(request) -> str:
    """屏幕上问一次审批。只负责显示和读一个答案，判断全在 permissions.py。"""
    _line()
    _line(request.question)
    for line in request.lines:
        _line(f"  {line}")
    if "a" in request.options:
        _line(f"  a = 本会话不再问：{request.remember_label}")
    shown = "/".join("N" if option == "n" else option for option in request.options)
    try:
        answer = input(f"[{shown}] ").strip().lower()
    except EOFError:
        answer = "n"
    return answer or "n"


def _prompt() -> str:
    """输入提示符：控制台编码放不下 › 时退回 >。"""
    try:
        "› ".encode(sys.stdout.encoding or "utf-8")
    except (LookupError, UnicodeEncodeError):
        return "> "
    return "› "


def _print_header(session: Session) -> None:
    _line(
        f"● {session.config['model']}  ·  {session.config['base_url']}"
        f"  ·  {session.permissions.mode}"
    )


# ---------------------------------------------------------------------------
# 主回合：模型 ↔ 工具；收尾时把子 agent 的报告收进来
# ---------------------------------------------------------------------------


def run_turn(messages: list[dict[str, Any]], task: str, session: Session) -> int:
    """把一条任务追加进历史再跑，跑完做回合收尾。

    中断或请求出错时，把这一轮追加的消息全撤掉：历史停在上一轮结束的位置，
    既不会留下「user 发了、assistant 没回」，也不会留下只调了工具没回结果的半截。
    同时给这一轮起的子 agent 发关闭请求——只请求，不等，别把中断卡在在途请求上。

    计划不跟着回退：它不属于历史，被中断的那一轮里 update_plan 做过的改动留着，
    下一轮请求照样会把最新清单带上。
    """
    session.stats.turns += 1
    turn = session.subagents.begin_turn()
    mark = len(messages)
    messages.append({"role": "user", "content": [{"type": "text", "text": task}]})
    try:
        outcome, held = _main_steps(messages, session, turn)
        return _wrap_up(messages, session, turn, outcome, held)
    except BaseException:
        del messages[mark:]
        session.subagents.request_close_turn(turn)
        raise


def _main_steps(
    messages: list[dict[str, Any]], session: Session, turn: int
) -> tuple[agent_loop.Outcome, list[str]]:
    """主 agent 的常规回合：跑到模型收尾，或者用满 MAX_STEPS。

    模型准备收尾的那段文本先压着不发（held）：这一回合还有子 agent 没交报告的话，
    等收齐了再让它综合；没有的话当场就发。
    """
    base = system_prompt()
    held: list[str] = []

    def system(step: int) -> str:
        # 每次请求前都从 plan 读一份新快照；只有这一轮的第一份请求带 hint
        return with_plan_context(base, session.plan, hint=step == 0)

    def on_text(text: str, final: bool) -> None:
        if final and session.subagents.has_pending(turn):
            held.append(text)
            return
        _render(text)
        if not final:
            _line()

    def execute(call: dict[str, Any]) -> dict[str, Any]:
        _line(f"→ {_describe_call(call)}")
        result = dispatch(call, session.permissions)
        mark = "失败" if result["is_error"] else "ok"
        _line(f"← {mark} · {len(result['content'])} 字符")
        session.stats.record_tool(str(call.get("name") or ""), bool(result["is_error"]))
        return result

    outcome = agent_loop.run_steps(
        messages,
        request=session.request,
        system=system,
        tools=TOOL_SCHEMAS,
        execute=execute,
        max_steps=MAX_STEPS,
        on_text=on_text,
        on_tools_done=_line,
    )
    return outcome, held


def _wrap_up(
    messages: list[dict[str, Any]],
    session: Session,
    turn: int,
    outcome: agent_loop.Outcome,
    held: list[str],
) -> int:
    """回合收尾：收子 agent 的报告，必要时再请求一次综合。

    - 正常收尾、还有没交付的报告：等 worker 全停 → 报告注入历史 → 综合 → 发最终答复；
    - 用满步数、还有没交付的报告：额外给一次综合请求（同样不带工具）；
    - 综合没成：把原始报告摆出来，明说这一回合没完成，不假装综合成功；
    - 其它失败：等 worker 停、把报告摆出来，再报失败原因。
    """
    if not session.subagents.has_pending(turn):
        _show_held(held)
        if outcome.problem:
            _line(outcome.problem, sys.stderr)
        return outcome.code

    reports = session.subagents.await_turn(turn)
    if not reports:
        _show_held(held)
        if outcome.problem:
            _line(outcome.problem, sys.stderr)
        return outcome.code

    report_text = format_reports(reports)
    if outcome.code == 0 or outcome.exhausted:
        try:
            answer = agent_loop.synthesize(
                messages,
                request=session.request,
                system=with_plan_context(system_prompt(), session.plan),
                report_text=report_text,
            )
        except RuntimeError as error:
            _show_unfinished(held, report_text, f"综合请求失败：{error}")
            return 1
        if answer:
            _render(answer)
            return 0
        _show_unfinished(held, report_text, "模型没有给出有效答复")
        return 1

    _show_unfinished(held, report_text, outcome.problem or "这一回合没跑完")
    return outcome.code


def _show_held(held: list[str]) -> None:
    """把压着的收尾文本发出去（只有原本该等报告、最后又没报告可等时才会用到）。"""
    for text in held:
        _render(text)
        _line()


def _show_unfinished(held: list[str], report_text: str, problem: str) -> None:
    """综合没成功：把手里有的东西原样摆出来，明确这一回合没完成。"""
    _line()
    _line(f"（本回合未完成：{problem}）", sys.stderr)
    if held:
        _line("（下面是综合之前模型说的话）", sys.stderr)
        for text in held:
            _render(text)
            _line()
    _line("（下面是子 agent 的原始报告）", sys.stderr)
    _render(report_text)
    _line()


# ---------------------------------------------------------------------------
# 一次性和对话框
# ---------------------------------------------------------------------------


def run(task: str, session: Session) -> int:
    """一次性：跑完就退出（退出码 0 完成 / 1 没跑完）。"""
    return run_turn([], task, session)


def repl(session: Session) -> int:
    """对话框：一行一条任务，历史一直留着，所以上下文能接着聊。

    历史是往下追加的，前面几轮的请求开头原样不动，
    模型服务端的前缀缓存就能接着用。

    计划跟这一场会话绑在一起：/new 换新对话时一起清空；
    界面要显示进度就读 plan 的快照，不要去聊天记录里找。
    子 agent 也属于这一场会话：/new 前先把它们关干净，再清记录。
    """
    messages: list[dict[str, Any]] = []

    _print_header(session)
    _line("输入任务回车发送；/help 看命令，/exit 退出。")
    _line()

    while True:
        try:
            line = input(_prompt()).strip()
        except EOFError:
            _line()
            return 0
        except KeyboardInterrupt:
            _line("（想退出打 /exit，或按 Ctrl-D）")
            continue

        if not line:
            continue
        if line in {"/exit", "/quit"}:
            return 0
        if line == "/help":
            _line("  /help   这条帮助")
            _line("  /plan   看当前计划（只读）")
            _line("  /new    清空历史、计划和子 agent，开一段新对话（上下文从头来）")
            _line("  /exit   退出（Ctrl-D 也行）")
            _line(f"  其它输入都当任务发给模型；每轮最多 {MAX_STEPS} 次模型请求，Ctrl-C 中断当前轮。")
            _line(f"  子 agent 由模型自己用 spawn_agent 派，只读，最多 {MAX_CONCURRENT} 个同时跑。")
            _line()
            continue
        if line == "/plan":
            _line(format_plan(session.plan.snapshot()))
            _line()
            continue
        if line == "/new":
            session.subagents.reset()
            messages.clear()
            session.plan.reset()
            _line("（历史和计划都已清空，还在跑的子 agent 也关掉了，接着输入任务）")
            _line()
            continue

        try:
            run_turn(messages, line, session)
        except KeyboardInterrupt:
            _line("（已中断这一轮，历史回到中断前）", sys.stderr)
        except RuntimeError as error:
            _line(f"✗ {error}", sys.stderr)
        _line()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="carrycode",
        description="CarryCode - 最小 agent loop：11 个工具，带对话框",
    )
    parser.add_argument("prompt", nargs="*", help="要交给模型的任务；不写就进对话框")
    parser.add_argument("--version", action="store_true", help="打印版本")
    parser.add_argument("--yes", action="store_true", help="常规审批不问，直接执行（风险自负）")
    parser.add_argument("--readonly", action="store_true", help="只读：写和执行一律拒绝")
    parser.add_argument("--allow-outside", action="store_true", help="放开工作区边界（写和读都放）")
    return parser


def _shutdown(session: Session) -> None:
    """退出前把子 agent 收干净。close 是协作式的，在途请求可能要等它返回或超时。"""
    try:
        session.subagents.close_all()
    except KeyboardInterrupt:
        _line("（又按了一次 Ctrl-C：不再等子 agent，直接退出）", sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="replace")

    if args.version:
        _line(f"CarryCode {__version__}")
        return 0

    task = " ".join(args.prompt).strip()

    config = load_config()
    if not config["api_key"]:
        _line("缺少 ANTHROPIC_API_KEY：写进 CarryCode\\.env，或设为环境变量。", sys.stderr)
        return 2

    if args.yes and args.readonly:
        _line("--yes 和 --readonly 不能同时用。", sys.stderr)
        return 2

    mode = "readonly" if args.readonly else ("auto" if args.yes else "ask")
    permissions = Permissions(
        mode=mode,
        asker=_ask_permission,
        workspace=Path.cwd(),
        allow_outside=args.allow_outside,
    )

    # 一场会话一个计划：agent loop、update_plan 工具、/plan 共用这一个实例，
    # 谁也不另外存一份。它只活在内存里，新会话从空清单开始。
    # 以后加 --resume 时只恢复 messages，不恢复计划（产品文档 S2 的决定）。
    plan = PlanManager()
    update_plan.bind(plan)

    stats = SessionStats(model=config["model"], workspace=str(Path.cwd()))
    model = Model(config)
    # 子 agent 和主 agent 共用这一份 Model（也就共用一把请求锁），
    # 状态行也从 _line 出去：整行、加锁，不跟主线程的渲染交错。
    subagents = SubAgentManager(
        model=model,
        permissions=permissions,
        workspace=Path.cwd(),
        stats=stats,
        emit=_line,
    )
    bind_agents(subagents)
    session = Session(
        config=config,
        model=model,
        permissions=permissions,
        plan=plan,
        subagents=subagents,
        stats=stats,
    )

    hooks = Hooks()
    hooks.on(SESSION_END, make_session_report_hook())
    hooks.emit(SESSION_START, stats=stats)

    if not rich_available():
        _line(
            "（没装 rich：markdown 按纯文本显示；pip install rich 后重开即可渲染）",
            sys.stderr,
        )

    try:
        if not task:
            # 没给任务就进对话框
            try:
                return repl(session)
            except KeyboardInterrupt:
                _line()
                return 0

        _print_header(session)
        _line()
        try:
            return run(task, session)
        except KeyboardInterrupt:
            _line("（已中断）", sys.stderr)
            return 130
        except RuntimeError as error:
            _line(f"✗ {error}", sys.stderr)
            return 1
    finally:
        # 先收子 agent，再出报告：报告里的请求数把子 agent 的也算进去。
        # 正常退出、Ctrl-C、请求失败都算会话结束，报告只出一次，放在这里最省心。
        _shutdown(session)
        hooks.emit(SESSION_END, stats=stats)


if __name__ == "__main__":
    raise SystemExit(main())
