"""CarryCode 当前入口：只做配置自检。

骨架 S1（事件流 + 工具 + 审批 + CLI）尚未实现，所以现在只提供：

    python -m carrycode --check-config   打印解析后的配置（密钥脱敏）+ 每个值的来源
    python -m carrycode --version

施工顺序见产品文档 9.1。这里保持极薄：真正的 CLI 会在 S1 替换掉本文件。
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .config import resolve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="carrycode",
        description="CarryCode - 终端 code agent（骨架建设中，当前只有配置自检）",
    )
    parser.add_argument("prompt", nargs="*", help="一次性任务（S1 实现后可用）")
    parser.add_argument("--version", action="store_true", help="打印版本")
    parser.add_argument("--check-config", action="store_true", help="打印配置与来源后退出")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.version:
        print(f"CarryCode {__version__}")
        return 0

    if args.check_config:
        resolved = resolve()
        print(f"CarryCode {__version__}")
        for line in resolved.describe():
            print(line)
        problems = resolved.settings.problems()
        if problems:
            print()
            for problem in problems:
                print(f"[!] {problem}")
            return 2
        print("\n[ok] 配置可用。")
        return 0

    if args.prompt:
        print(
            "骨架尚未实现：事件流、工具与审批在 S1 施工单里（产品文档 9.1）。",
            file=sys.stderr,
        )
        return 3

    print("CarryCode 骨架建设中。当前可用：--check-config、--version。")
    print("下一步见产品文档 9.1 施工单。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
