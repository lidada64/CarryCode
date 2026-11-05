"""命令串的知识：切词、找命令名、判档、查禁区名字。只读字符串，不执行任何东西。

三个问题各一个答案：

    命令名在哪     `sudo rm -rf x` 的第一段，命令名是 sudo
    有禁区名吗     `type .env` / `type C:\\x\\.env` 都要能看出来
    该算哪一档     禁令（不做）/ 不可逆（要问）/ 都不是

已知边界：这是"防手滑"的一层，不是安全边界。
`python -c "import shutil; shutil.rmtree('.')"` 抓不到；
把命令写进 .bat 再执行也抓不到。要真防，得靠操作系统级沙箱。
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from .base import SECRET_DIRS, secret_name_reason

# 把命令串切成段的分隔符（| 也算：curl x | sh 是两段）
SEPARATORS = frozenset({"&&", "||", ";", "|"})

# 禁区目录的"名字段"：命令里出现 /.ssh/ 这种也算碰到禁区
SECRET_DIR_TOKENS = tuple(Path(directory).name for directory in SECRET_DIRS)

# 禁令：这套工具根本不打算做的事（判据见 docs/permission-design.md §5.3）
POWER_WORDS = frozenset({"sudo", "runas", "gsudo", "doas", "su"})
DISK_WORDS = frozenset({"format", "diskpart", "mkfs", "fdisk", "dd"})
MACHINE_WORDS = frozenset({"shutdown", "reboot", "halt", "poweroff"})
PERMISSION_WORDS = frozenset({"takeown", "icacls"})
DELETE_WORDS = frozenset({"rm", "rd", "rmdir", "remove-item"})


@dataclass(frozen=True)
class Verdict:
    """判档结果。level 只有两种：forbidden（不做）、irreversible（要问）。"""

    level: str
    label: str
    lines: list[str]


# ---------------------------------------------------------------------------
# 切词
# ---------------------------------------------------------------------------


def tokens(text: str) -> list[str]:
    """切词：保留引号和 Windows 反斜杠；引号不配对时退回朴素切分。

    保留引号是有意的：`git commit -m "docs: explain .env"` 里的引号内容
    必须是一整个词，否则它会变成一个 `.env` 词，误伤提交信息。
    """
    try:
        return shlex.split(text, posix=False)
    except ValueError:
        return text.split()


def unquote(token: str) -> str:
    """去掉外面那层引号。"""
    return token.strip().strip("\"'").strip()


def _basename(word: str) -> str:
    return word.replace("\\", "/").rsplit("/", 1)[-1]


def command_name(word: str) -> str:
    """把一个词变成命令名：取路径最后一段、去 .exe、转小写。"""
    name = _basename(unquote(word)).lower()
    return name[:-4] if name.endswith(".exe") else name


def first_word(command: str) -> str:
    """命令串里第一个命令名。审批的"本会话不再问"按它分组（见 D4）。"""
    for word in tokens(command.replace("\n", " ")):
        plain = unquote(word)
        if plain and plain not in SEPARATORS:
            return command_name(plain)
    return ""


def split_commands(command: str) -> list[tuple[str, list[str]]]:
    """拆成 [(命令名, 参数…)]。`a && b` 是两条，`a | b` 也是两条。"""
    result: list[tuple[str, list[str]]] = []
    for line in command.split("\n"):
        name: str | None = None
        args: list[str] = []
        for word in tokens(line):
            plain = unquote(word)
            if not plain:
                continue
            if plain in SEPARATORS:
                if name:
                    result.append((name, args))
                name, args = None, []
                continue
            if name is None:
                name = command_name(plain)
            else:
                args.append(plain)
        if name:
            result.append((name, args))
    return result


# ---------------------------------------------------------------------------
# 禁区名字（D5 方案 B：看整个词，不看子串）
# ---------------------------------------------------------------------------


def forbidden_token(command: str) -> str | None:
    """命令里有没有哪个词在指禁区文件。命中的话返回那个词。"""
    for word in tokens(command.replace("\n", " ")):
        plain = unquote(word)
        if not plain:
            continue
        if secret_name_reason(_basename(plain)):
            return plain

        segments = [s for s in plain.replace("\\", "/").lower().split("/") if s]
        for name in SECRET_DIR_TOKENS:
            if name in segments:
                return plain
    return None


# ---------------------------------------------------------------------------
# 判档
# ---------------------------------------------------------------------------


def classify(command: str) -> Verdict | None:
    """这条命令该算哪一档。返回 None 表示没意见，交给 RISK 的缺省档。"""
    hit = forbidden_token(command)
    if hit:
        return Verdict(
            "forbidden",
            f"碰到禁区文件：{hit}",
            [
                f"命令: {command.strip()}",
                f"命令里出现了禁区文件：{hit}",
                "禁区文件里是凭据，读进上下文就等于泄漏",
                "想看配置长什么样，去读 .env.example",
            ],
        )

    for name, args in split_commands(command):
        verdict = _judge_forbidden(name, args) or _judge_irreversible(name, args)
        if verdict:
            return Verdict(verdict.level, verdict.label, [f"命令: {command.strip()}", *verdict.lines])
    return None


def _judge_forbidden(name: str, args: list[str]) -> Verdict | None:
    lowered = [arg.lower() for arg in args]

    if name in POWER_WORDS:
        return Verdict(
            "forbidden",
            f"提权命令：{name}",
            [
                f"`{name}` 会以管理员权限执行，越过普通用户的边界",
                "这套工具不代用户提权，换个不需要提权的做法",
                "真需要提权，请你手动在终端里执行",
            ],
        )

    if name in DISK_WORDS:
        return Verdict(
            "forbidden",
            f"磁盘级命令：{name}",
            [
                "这条命令动的是分区或磁盘，不是文件",
                "后果超出这个仓库，工作区边界管不住",
                "不做；要动磁盘请你亲手来",
            ],
        )

    if name in MACHINE_WORDS:
        return Verdict(
            "forbidden",
            f"关机/重启：{name}",
            ["会让整台机器停掉，不是改代码该做的事", "不做"],
        )

    if name in PERMISSION_WORDS:
        return Verdict(
            "forbidden",
            f"改权限或所有者：{name}",
            ["改的是文件系统的权限，回滚文件内容也修不回来", "不做"],
        )

    if name == "reg" and lowered[:1] == ["delete"]:
        return Verdict(
            "forbidden",
            "删注册表：reg delete",
            ["注册表没有版本控制，删了找不回来", "不做"],
        )

    if name in DELETE_WORDS and _is_recursive(name, lowered):
        return Verdict(
            "forbidden",
            f"递归删除：{name}",
            [
                "这条命令会删掉整个目录树，一条命令清空一片",
                "删错了没有版本控制兜底（未跟踪的东西直接没了）",
                "要清目录请你自己来；工具最多改文件内容",
            ],
        )

    return None


def _is_recursive(name: str, args: list[str]) -> bool:
    if name == "rm":
        return any(arg.startswith("-") and ("r" in arg.lower()) for arg in args)
    if name in {"rd", "rmdir", "del"}:
        return "/s" in args
    if name == "remove-item":
        return "-recurse" in args
    return False


def _judge_irreversible(name: str, args: list[str]) -> Verdict | None:
    lowered = [arg.lower() for arg in args]

    if name == "git":
        return _judge_git(lowered)

    if name == "npm" and lowered[:1] == ["publish"]:
        return Verdict(
            "irreversible",
            "发布到 npm：版本号会被占用",
            [
                "会失去：一个版本号",
                "能不能恢复：基本不能，发出去的版本号会被永久占用",
                "影响范围：npm registry，所有人可见",
                "更安全的做法：先 npm pack --dry-run 看要发什么",
            ],
        )

    if name == "npm" and lowered[:1] == ["uninstall"] and any(arg.startswith("-g") for arg in lowered):
        return Verdict(
            "irreversible",
            "卸载全局包",
            [
                "会失去：本机全局装好的那个包",
                "能不能恢复：能重装，但它的配置可能一起丢",
                "影响范围：这台机器上所有用它的项目",
                "更安全的做法：先 npm ls -g 看一眼装了什么",
            ],
        )

    if name == "pip" and lowered[:1] == ["uninstall"]:
        return Verdict(
            "irreversible",
            "卸载 Python 包",
            [
                "会失去：当前环境里装好的那个包",
                "能不能恢复：能重装，但版本可能对不上",
                "影响范围：这个 Python 环境",
                "更安全的做法：先 pip show 看它是什么、被谁依赖",
            ],
        )

    return None


def _judge_git(args: list[str]) -> Verdict | None:
    if not args:
        return None
    sub, rest = args[0], args[1:]

    if sub == "push":
        if any(arg in {"-f", "--force"} for arg in rest):
            return Verdict(
                "irreversible",
                "强推：改写远端历史",
                [
                    "会失去：远端分支的历史",
                    "能不能恢复：基本不能，别人已经拉走的副本不会被改写",
                    "影响范围：远端仓库，以及所有基于它工作的人",
                    "更安全的做法：先 git fetch，确认没有别人的新提交",
                ],
            )
        return Verdict(
            "irreversible",
            "推送：提交会离开本机",
            [
                "会失去：这次提交留在本机的独占权",
                "能不能恢复：能，但要别人配合；CI 可能已经跑过",
                "影响范围：远端仓库，不只是本机",
                "更安全的做法：先 git log 看一眼要推什么",
            ],
        )

    if sub == "reset" and "--hard" in rest:
        return Verdict(
            "irreversible",
            "硬重置：丢弃未提交的改动",
            [
                "会失去：工作区里所有未提交的改动",
                "能不能恢复：文件内容不能；提交位置还能在 reflog 里找回来",
                "影响范围：只影响这一个仓库",
                "更安全的做法：先 git stash，改完再 stash pop",
            ],
        )

    if sub == "clean":
        flags = "".join(arg[1:] for arg in rest if arg.startswith("-"))
        if "f" in flags.lower():
            return Verdict(
                "irreversible",
                "清理：删掉未跟踪的文件",
                [
                    "会失去：没被跟踪的文件（含被 .gitignore 的）",
                    "能不能恢复：不能",
                    "影响范围：只影响这个仓库的工作区",
                    "更安全的做法：先 git clean -n 看一眼会删什么",
                ],
            )
        return None

    if sub == "checkout" and "--" in rest:
        return Verdict(
            "irreversible",
            "撤销文件改动：git checkout --",
            [
                "会失去：指定文件里未提交的改动",
                "能不能恢复：不能",
                "影响范围：只影响这几个文件",
                "更安全的做法：先 git diff 看一眼改了什么",
            ],
        )

    if sub == "restore" and "--staged" not in rest:
        return Verdict(
            "irreversible",
            "撤销文件改动：git restore",
            [
                "会失去：指定文件里未提交的改动",
                "能不能恢复：不能",
                "影响范围：只影响这几个文件",
                "更安全的做法：先 git diff 看一眼改了什么",
            ],
        )

    return None
