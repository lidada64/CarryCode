"""审批：所有工具调用都要先经过这里。

分工（详见 docs/permission-design.md）：

    工具         说这一次有多危险：RISK / inspect() / preview()
    permissions  决定"要不要问"：模式、边界、记忆
    界面         问人：asker 是外面传进来的 callable，这里不 print、不 input

三档模式：

    ask       默认：有副作用的调用先问
    auto      --yes：常规调用不问；不可逆和出工作区的读仍然问一次
    readonly  --readonly：只读，写和执行一律拒

判定顺序：

    0  未知工具（在 dispatch 里拦，不进这里）
    1  取档位
    2  永久禁止：禁区文件、禁区目录、禁令命令 → 拒，不问
    3  写越界 → 拒（硬边界，只有 allow_outside 能开）
    4  readonly 且不是 read → 拒
    5  必须问：不可逆、读越界 → 问，只给 y/N（模式和记忆都不管用）
    6  auto → 允许
    7  没有 asker → 拒
    8  记忆命中 → 允许 / 拒
    9  问：y / a / N / d
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .tools.base import is_within, secret_path_reason
from .tools.commands import first_word

MODES = ("ask", "auto", "readonly")

# 锁死的问句只给 y/N；常规问句给四个
OPTIONS_LOCKED = ("y", "n")
OPTIONS_FULL = ("y", "a", "n", "d")

# 用户怎么答。只认这几个词，其它一律当拒绝——"allow" 不该被猜成"一直允许"。
ANSWERS = {
    "y": "y", "yes": "y",
    "a": "a", "always": "a",
    "n": "n", "no": "n", "": "n",
    "d": "d", "deny": "d", "never": "d",
}

# 档位高低：低档不许被 inspect() 报成更低（"把危险的说轻"是最坏的错）
RANKS = {"read": 0, "write": 1, "exec": 1, "irreversible": 2, "forbidden": 3}

MSG_USER_DENIED = "denied by the user. Ask what they want, or propose a smaller step."
MSG_REMEMBERED = "denied: the user rejected this kind earlier in the session, so it will not be asked again."
MSG_NO_TTY = "denied: this call needs approval, but there is no interactive terminal."
MSG_READONLY = "denied: read-only mode; writes and commands are disabled. Use read-only tools instead."
MSG_OUTSIDE = "denied: that path is outside the workspace. Only the user can open it up (--allow-outside)."
MSG_FORBIDDEN = "denied: this operation is never allowed by this tool. Choose a different approach."
MSG_BAD_INSPECT = "denied: the tool reported a lower risk than it declared. This is a bug, not your fault."


@dataclass(frozen=True)
class Request:
    """一次要问人的审批。asker 拿到它负责显示，然后返回 y / a / n / d。"""

    tool: str
    risk: str
    question: str
    lines: list[str]
    options: tuple[str, ...]
    remember_label: str = ""


@dataclass(frozen=True)
class Decision:
    """审批结论。拒绝时 message 是给模型看的那句话。"""

    allowed: bool
    message: str = ""
    asked: bool = False


class Permissions:
    def __init__(
        self,
        mode: str = "ask",
        asker: Callable[[Request], str] | None = None,
        emit: Callable[[Request], None] | None = None,
        workspace: Path | None = None,
        allow_outside: bool = False,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"不认识的模式：{mode}（允许：{'、'.join(MODES)}）")
        self.mode = mode
        self.asker = asker
        self.emit = emit
        self.workspace = (workspace or Path.cwd()).resolve()
        self.allow_outside = allow_outside
        self._allowed: set[str] = set()
        self._denied: set[str] = set()

    # ------------------------------------------------------------------
    # 主判定
    # ------------------------------------------------------------------

    def check(self, tool: str, args: dict[str, Any], spec: dict[str, Any]) -> Decision:
        risk, lines = self._assess(tool, args, spec)

        # 2. 永久禁止（禁令命令、禁区文件）
        if risk == "forbidden":
            return Decision(False, MSG_FORBIDDEN)

        targets = self._targets(args, spec)
        for target in targets:
            reason = secret_path_reason(target)
            if reason:
                return Decision(False, f"denied: {reason}. Do not look for another way to read it.")

        outside = [target for target in targets if not is_within(self.workspace, target)]

        # 3. 写越界是硬边界
        if outside and risk != "read" and not self.allow_outside:
            return Decision(False, MSG_OUTSIDE)

        # 4. 只读模式
        if self.mode == "readonly" and risk != "read":
            return Decision(False, MSG_READONLY)

        # 5. 必须问的两件事：不可逆、读越界（给了 allow_outside 就不算越界）
        if risk == "irreversible":
            return self._ask(
                tool, args, risk, "要执行一条不可撤销的命令，允许吗？", lines, locked=True
            )
        if outside and not self.allow_outside:
            return self._ask(
                tool, args, risk, "要读工作区以外的文件，允许吗？", lines, locked=True
            )

        # 5.5 只读、没越界：不问
        if risk == "read":
            return Decision(True)

        # 6. 全不问模式
        if self.mode == "auto":
            return Decision(True)

        # 7. 没有人可以问
        if self.asker is None:
            return Decision(False, MSG_NO_TTY)

        # 8. 记忆
        key = self._key(tool, args)
        if key in self._denied:
            return Decision(False, MSG_REMEMBERED)
        if key in self._allowed:
            return Decision(True)

        # 9. 问
        return self._ask(tool, args, risk, self._question(tool, risk), lines, locked=False)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _assess(
        self, tool: str, args: dict[str, Any], spec: dict[str, Any]
    ) -> tuple[str, list[str]]:
        declared = spec.get("risk", "write")

        inspect = spec.get("inspect")
        if inspect is not None:
            level, lines = inspect(args)
            if level not in RANKS:
                level = declared
            if RANKS[level] < RANKS.get(declared, 1):
                # 工具把自己的危险说轻了：按错误处理，不按放行处理
                return "forbidden", [f"工具 {tool} 报的档位比声明的低：{level} < {declared}"]
            return level, list(lines)

        preview = spec.get("preview")
        lines: list[str] = []
        if preview is not None:
            try:
                lines = list(preview(args))
            except Exception:  # 预览出错不该把这次调用弄挂
                lines = []
        if not lines:
            lines = [f"工具: {tool}"]
        return declared, lines

    def _targets(self, args: dict[str, Any], spec: dict[str, Any]) -> list[Path]:
        result: list[Path] = []
        for name in spec.get("paths") or ():
            raw = args.get(name)
            if isinstance(raw, str) and raw.strip():
                result.append(self._resolve(raw))
        return result

    def _resolve(self, raw: str) -> Path:
        target = Path(raw).expanduser()
        if not target.is_absolute():
            target = self.workspace / target
        try:
            return target.resolve()
        except OSError:
            return target

    def _key(self, tool: str, args: dict[str, Any]) -> str:
        """记忆的键。bash 按命令的第一个词分组，其它工具按工具名（D4 方案 B）。"""
        if tool == "bash":
            return f"bash::{first_word(str(args.get('command') or '')) or '*'}"
        return tool

    def _label(self, tool: str, args: dict[str, Any]) -> str:
        """审批块上写清 a / d 到底记住什么，别让人猜。"""
        if tool == "bash":
            word = first_word(str(args.get("command") or ""))
            return f"所有 {word} 命令" if word else "所有命令"
        return tool

    def _question(self, tool: str, risk: str) -> str:
        if risk == "write":
            return "要改动文件，允许吗？"
        if risk in {"exec", "irreversible"}:
            return "要执行命令，允许吗？"
        return f"要执行 {tool}，允许吗？"

    def _ask(
        self,
        tool: str,
        args: dict[str, Any],
        risk: str,
        question: str,
        lines: list[str],
        *,
        locked: bool,
    ) -> Decision:
        if self.asker is None:
            return Decision(False, MSG_NO_TTY)

        options = OPTIONS_LOCKED if locked else OPTIONS_FULL
        request = Request(
            tool=tool,
            risk=risk,
            question=question,
            lines=list(lines),
            options=options,
            remember_label=self._label(tool, args),
        )
        if self.emit is not None:
            self.emit(request)

        answer = ANSWERS.get(str(self.asker(request) or "").strip().lower(), "n")
        if answer not in options:
            answer = "n"

        if answer == "y":
            return Decision(True, asked=True)
        if answer == "a":
            self._allowed.add(self._key(tool, args))
            return Decision(True, asked=True)
        if answer == "d":
            self._denied.add(self._key(tool, args))
            return Decision(False, MSG_REMEMBERED, asked=True)
        return Decision(False, MSG_USER_DENIED, asked=True)
