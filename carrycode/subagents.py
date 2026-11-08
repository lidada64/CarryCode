"""子 agent：内存里的记录、线程、独立消息、状态和收尾。

一场会话一个 SubAgentManager，最多 MAX_CONCURRENT 个 worker 同时跑。
worker 复用主 agent 的 agent_loop 和同一份 Model（也就共用一把请求锁），
但消息、system、工具清单都是自己的一份，碰不到主对话和计划。

分工：

    subagents.py   记录、线程、等待与关闭、报告拼装
    agent_loop.py  worker 跑的循环（和主 agent 同一个）
    model.py       一次请求（主、子共用一把锁）
    tools/         四个管理工具只做参数校验，状态全在这里

线程模型：

    spawn 之后主线程立刻返回；worker 在自己的线程里跑，退出时 set 一下 done 事件。
    主线程只在两处等 worker：wait_agent 工具和回合收尾。等的时候不持锁——
    拿着锁等线程，worker 结束时拿不到锁就是死锁。

关闭：

    给 cancel 置位，worker 在「下一个请求之前」「等模型锁的时候」看它；
    已经在路上的同步请求没法强杀，只能等它返回或超时，所以 close 会一直等线程真的退出。
    线程是 daemon：正常退出前 close_all 会等它们收干净；真被强杀时也不拖着进程。

审批：

    worker 拿的是另一份 Permissions（asker=None）。读工作区里的文件不问；
    需要问的（比如读工作区外面）一律拒——后台线程不该抢终端的输入。
    白名单在 schema 和执行层各拦一次：模型伪造别的工具名也进不了 dispatch。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import agent_loop
from .permissions import Permissions
from .tools import TOOLS, dispatch

MAX_CONCURRENT = 3  # 同时最多几个 worker
WORKER_MAX_STEPS = 20  # 一个 worker 最多几次模型请求（参照 MiniCode）

# worker 能用的工具：schema 里只发这三个，执行层再拦一次
WORKER_TOOL_NAMES = ("read_file", "glob", "grep")
WORKER_SCHEMAS = [{"name": name, **TOOLS[name]["schema"]} for name in WORKER_TOOL_NAMES]

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CLOSED = "closed"
TERMINAL_STATUSES = (STATUS_COMPLETED, STATUS_FAILED, STATUS_CLOSED)

STATUS_LABELS = {
    STATUS_COMPLETED: "完成",
    STATUS_FAILED: "失败",
    STATUS_CLOSED: "已关闭",
}

DEFAULT_WAIT_MS = 30_000


def worker_system(workspace: str) -> str:
    """worker 的 system：只读、只干交给它的那一件事、最后交一份带证据的报告。"""
    return f"""你是 CarryCode 派出的只读子 agent，只做调查，不改代码。

当前环境：
- 工作区：{workspace}

规则：
1. 只查用户消息里交给你的那一件事，不要顺手扩大范围。
2. 只能用 read_file、glob、grep 拿证据；调不了 bash，也改不了文件、起不了别的子 agent。
3. 结论要落到证据上：写清文件路径、行号，必要时带上原文片段。
4. 最后给一份简短报告：结论、证据（路径:行号）、还没搞清的地方。
5. 不要说改过任何文件——改代码是主 agent 的事。用中文写。
"""


@dataclass
class _Record:
    """一个 worker 的全部状态。字段只在 manager 的锁里改，done 事件除外。"""

    agent_id: str
    task: str
    turn: int
    status: str = STATUS_RUNNING
    result: str = ""
    error: str = ""
    delivered: bool = False
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


class SubAgentManager:
    def __init__(
        self,
        *,
        model: Any,
        permissions: Permissions,
        workspace: str | Path | None = None,
        stats: Any = None,
        emit: Callable[[str], None] | None = None,
        max_concurrent: int = MAX_CONCURRENT,
        max_steps: int = WORKER_MAX_STEPS,
    ) -> None:
        self._model = model
        self._permissions = _worker_permissions(permissions)
        self._workspace = str(workspace or Path.cwd())
        self._stats = stats
        self._emit_event = emit
        self._max_concurrent = max_concurrent
        self._max_steps = max_steps
        self._lock = threading.Lock()
        self._records: dict[str, _Record] = {}
        self._next_id = 0
        self._turn = 0

    # ------------------------------------------------------------------
    # 回合：记录按「哪个主回合创建」归档，收尾时只交付本回合的
    # ------------------------------------------------------------------

    def begin_turn(self) -> int:
        """主 agent 开一个新回合，返回它的编号。之后 spawn 都记在这个回合名下。"""
        with self._lock:
            self._turn += 1
            return self._turn

    # ------------------------------------------------------------------
    # 给工具用的四个动作
    # ------------------------------------------------------------------

    def spawn(self, task: str) -> dict[str, Any]:
        """起一个 worker 并立刻返回它的快照；任务为空或并发满了抛 ValueError。"""
        task = str(task or "").strip()
        if not task:
            raise ValueError("task 不能为空：说清楚要子 agent 查什么。")

        with self._lock:
            if self._running_locked() >= self._max_concurrent:
                raise ValueError(
                    f"最多 {self._max_concurrent} 个子 agent 同时跑；"
                    "等一个结束，或者先 close_agent。"
                )
            self._next_id += 1
            record = _Record(
                agent_id=f"agent_{self._next_id}",
                task=task,
                turn=self._turn,
            )
            self._records[record.agent_id] = record
            snapshot = self._public(record)

        # 启动提示要赶在线程真的跑起来之前发，顺序才对得上
        self._emit(f"→ 启动子 agent {record.agent_id}：{task}")
        record.thread = threading.Thread(
            target=self._run,
            args=(record,),
            name=record.agent_id,
            daemon=True,
        )
        record.thread.start()
        return snapshot

    def records(self) -> list[dict[str, Any]]:
        """本会话创建过的全部 worker（含报告和错误），按创建顺序。"""
        with self._lock:
            return [self._public(record) for record in self._records.values()]

    def wait(
        self, agent_ids: list[str], timeout_ms: int = DEFAULT_WAIT_MS
    ) -> tuple[bool, list[dict[str, Any]]]:
        """等这些 worker 全停，最多 timeout_ms 毫秒。

        返回 (是不是超时, 快照列表)。超时不动它们；已经到终态的记录顺便记为已交付。
        """
        with self._lock:
            records = [self._require(agent_id) for agent_id in agent_ids]

        deadline = time.monotonic() + max(0, timeout_ms) / 1000
        timed_out = False
        for record in records:
            if record.done.is_set():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not record.done.wait(remaining):
                timed_out = True
                break

        with self._lock:
            for record in records:
                if record.status in TERMINAL_STATUSES:
                    record.delivered = True
            return timed_out, [self._public(record) for record in records]

    def close(self, agent_id: str) -> dict[str, Any]:
        """请求关闭并等它真的退出，返回最终状态；已经不跑了的直接返回。

        和 wait 一样算「交给主 agent 了」：close_agent 的返回值里就带着最终状态
        （已经跑完的还带着报告），回合收尾时不该再注入一遍。
        """
        with self._lock:
            record = self._require(agent_id)
        record.cancel.set()
        record.done.wait()
        with self._lock:
            if record.status in TERMINAL_STATUSES:
                record.delivered = True
            return self._public(record)

    # ------------------------------------------------------------------
    # 主回合收尾
    # ------------------------------------------------------------------

    def has_pending(self, turn: int) -> bool:
        """这个回合还有没交付的东西吗（还在跑的，或者跑完了但没交给主 agent 的）。"""
        with self._lock:
            return any(
                record.turn == turn and not record.delivered
                for record in self._records.values()
            )

    def await_turn(self, turn: int) -> list[dict[str, Any]]:
        """等这个回合的 worker 全停，取还没交付的终态记录（取过就记为已交付）。

        等的时候不持锁；Ctrl-C 会从这里穿过去，由调用方决定回滚和关闭。
        """
        with self._lock:
            records = [record for record in self._records.values() if record.turn == turn]
        for record in records:
            record.done.wait()
        return self._collect(records)

    def request_close_turn(self, turn: int) -> None:
        """只发关闭请求，不等：Ctrl-C 时用，别把中断卡在一个在途请求上。"""
        with self._lock:
            records = [record for record in self._records.values() if record.turn == turn]
        for record in records:
            record.cancel.set()

    def close_all(self) -> None:
        """请求关闭所有还在跑的 worker，并等它们真的退出。"""
        with self._lock:
            records = list(self._records.values())
        for record in records:
            record.cancel.set()
        for record in records:
            record.done.wait()

    def reset(self) -> None:
        """换一段新对话：先把 worker 停干净，再清空记录。"""
        self.close_all()
        with self._lock:
            self._records.clear()
            self._turn = 0

    # ------------------------------------------------------------------
    # 只读属性（工具用）
    # ------------------------------------------------------------------

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def running_count(self) -> int:
        with self._lock:
            return self._running_locked()

    # ------------------------------------------------------------------
    # 内部：线程、请求、白名单、状态
    # ------------------------------------------------------------------

    def _run(self, record: _Record) -> None:
        try:
            prompt = worker_system(self._workspace)
            messages: list[dict[str, Any]] = [
                {"role": "user", "content": [{"type": "text", "text": record.task}]}
            ]
            outcome = agent_loop.run_steps(
                messages,
                request=lambda msgs, system, tools: self._request(record, msgs, system, tools),
                system=lambda step: prompt,
                tools=WORKER_SCHEMAS,
                execute=self._execute,
                max_steps=self._max_steps,
                cancel=record.cancel,
            )
            if outcome.code == 0:
                self._finish(record, STATUS_COMPLETED, result=outcome.final_text.strip())
            else:
                self._finish(record, STATUS_FAILED, error=outcome.problem or "子 agent 没跑完。")
        except BaseException as error:  # 取消、请求失败、线程里的意外
            if record.cancel.is_set():
                self._finish(record, STATUS_CLOSED)
            else:
                self._finish(record, STATUS_FAILED, error=f"{type(error).__name__}: {error}")
        finally:
            record.done.set()

    def _request(
        self,
        record: _Record,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """worker 的一次请求。取消事件跟着进 model.py，等锁的时候也能停。"""
        if self._stats is not None:
            self._stats.record_request()
        data = self._model.call(messages, system, tools, record.cancel)
        if self._stats is not None:
            self._stats.record_usage(data.get("usage"))
        return data

    def _execute(self, call: dict[str, Any]) -> dict[str, Any]:
        """执行层的白名单：模型伪造别的工具名，也到不了 dispatch。"""
        name = str(call.get("name") or "")
        if name not in WORKER_TOOL_NAMES:
            allowed = "、".join(WORKER_TOOL_NAMES)
            result = {
                "type": "tool_result",
                "tool_use_id": call.get("id", ""),
                "content": f"子 agent 只能用 {allowed}，不能用 {name or '这个工具'}。",
                "is_error": True,
            }
        else:
            result = dispatch(call, self._permissions)
        if self._stats is not None:
            self._stats.record_tool(name, bool(result["is_error"]))
        return result

    def _finish(self, record: _Record, status: str, *, result: str = "", error: str = "") -> None:
        with self._lock:
            if record.status != STATUS_RUNNING:
                return
            if record.cancel.is_set() and status != STATUS_CLOSED:
                # 关闭请求先到：哪怕它正好跑完了，也按关闭算，别给主 agent 交一份没人要的报告
                status, result, error = STATUS_CLOSED, "", ""
            record.status = status
            record.result = result
            record.error = error

        line = f"← {record.agent_id} {STATUS_LABELS[status]}"
        if status == STATUS_FAILED and error:
            # 终端只放一句摘要；完整错误在记录里，wait_agent / list_agents 拿得到
            brief = error if len(error) <= 120 else error[:120] + "…"
            line += f"：{brief}"
        self._emit(line)

    def _collect(self, records: list[_Record]) -> list[dict[str, Any]]:
        with self._lock:
            fresh = [
                record
                for record in records
                if record.status in TERMINAL_STATUSES and not record.delivered
            ]
            for record in fresh:
                record.delivered = True
            return [self._public(record) for record in fresh]

    def _require(self, agent_id: str) -> _Record:
        record = self._records.get(agent_id)
        if record is None:
            known = "、".join(self._records) or "（还没有）"
            raise ValueError(f"没有这个子 agent：{agent_id}。本会话创建过：{known}")
        return record

    def _running_locked(self) -> int:
        return sum(
            1 for record in self._records.values() if record.status == STATUS_RUNNING
        )

    @staticmethod
    def _public(record: _Record) -> dict[str, Any]:
        return {
            "agent_id": record.agent_id,
            "task": record.task,
            "status": record.status,
            "result": record.result,
            "error": record.error,
        }

    def _emit(self, line: str) -> None:
        if self._emit_event is None:
            return
        try:
            self._emit_event(line)
        except Exception:  # 终端输出出问题不该影响 worker
            pass


# ---------------------------------------------------------------------------
# 报告与权限：给人看的、给模型看的都是这一份
# ---------------------------------------------------------------------------


def format_reports(records: list[dict[str, Any]]) -> str:
    """把记录拼成报告。综合请求用它当输入，综合失败时也用它给人看。"""
    sections: list[str] = []
    for record in records:
        status = str(record.get("status") or "")
        lines = [
            f"### {record.get('agent_id', '')} · {STATUS_LABELS.get(status, status)}",
            f"任务：{record.get('task', '')}",
        ]
        if status == STATUS_COMPLETED:
            lines.append("报告：")
            lines.append(str(record.get("result") or "（没有写报告）"))
        elif status == STATUS_FAILED:
            lines.append(f"错误：{record.get('error') or '（没有错误信息）'}")
        else:
            lines.append("关闭请求之后没有再继续调查，没有报告。")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _worker_permissions(permissions: Permissions) -> Permissions:
    """worker 的那一份：同样的模式和边界，但没有人可以问（asker=None）。"""
    return Permissions(
        mode=permissions.mode,
        asker=None,
        workspace=permissions.workspace,
        allow_outside=permissions.allow_outside,
    )
