"""edit_file 工具：把一段唯一出现的原文替换掉；不唯一就报错，不去猜。"""

from __future__ import annotations

from typing import Any

from .base import read_text, resolve_path, write_text

NAME = "edit_file"

SCHEMA = {
    "description": (
        "把文件里一段原文替换成新内容。old_string 必须在文件里唯一出现，"
        "并且和文件里的字符完全一致（含缩进、空格、换行）。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径，相对路径按当前工作目录算",
            },
            "old_string": {
                "type": "string",
                "description": "要被替换的原文，最好带上几行上下文保证唯一",
            },
            "new_string": {
                "type": "string",
                "description": "替换成的新内容，可以是空字符串（等于删掉这段）",
            },
            "replace_all": {
                "type": "boolean",
                "description": "上面那段出现多次时是否全部替换，默认 false",
            },
        },
        "required": ["path", "old_string", "new_string"],
    },
}


def run(args: dict[str, Any]) -> tuple[str, bool]:
    path = str(args.get("path") or "").strip()
    if not path:
        return "缺少 path 参数。", True
    old = args.get("old_string")
    new = args.get("new_string")
    if not isinstance(old, str) or not old:
        return "缺少 old_string（要替换的原文，不能是空字符串）。", True
    if not isinstance(new, str):
        return "缺少 new_string（替换成什么，可以是空字符串）。", True

    target = resolve_path(path)
    if not target.exists():
        return f"文件不存在：{path}", True
    if target.is_dir():
        return f"{path} 是目录，不能编辑。", True

    try:
        text, crlf = read_text(target)
    except OSError as error:
        return f"读不了 {path}：{error}", True

    occurrences = text.count(old)
    if occurrences == 0:
        return (
            f"在 {path} 里找不到 old_string。注意缩进、空格、换行要和文件完全一致；"
            "也可能是这段内容已经被改过或删掉了，先 read_file 看一眼。",
            True,
        )

    replace_all = bool(args.get("replace_all"))
    if occurrences > 1 and not replace_all:
        return (
            f"old_string 在 {path} 里出现了 {occurrences} 次，无法确定改哪一处。"
            "带上更多上下文让它唯一，或者传 replace_all=true 全部替换。",
            True,
        )

    first = text.index(old)
    line_number = text.count("\n", 0, first) + 1
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)

    try:
        write_text(target, updated, crlf)
    except OSError as error:
        return f"写不了 {path}：{error}", True

    changed = occurrences if replace_all else 1
    return f"已修改 {path}：替换 {changed} 处（起始第 {line_number} 行）。", False
