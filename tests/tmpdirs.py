"""测试用临时目录。

为什么不用 `tempfile.mkdtemp`：Windows 上它会以 0700 建目录（相当于额外设了
DACL），在当前沙箱环境里这种目录会被拒绝写入，导致与测试逻辑无关的
PermissionError。这里用普通 mkdir，权限继承父目录，读写都正常。

临时目录必须留在仓库内：沙箱禁止在工作区外写文件，而用例本身要造 .env。
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

TMP_ROOT = Path(__file__).resolve().parent / ".tmp"


def make_temp_dir(prefix: str) -> Path:
    """在 tests/.tmp/ 下建一个唯一目录并返回。"""
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = TMP_ROOT / f"{prefix}-{uuid.uuid4().hex[:8]}"
    path.mkdir()
    return path


def cleanup(path: Path) -> None:
    """尽力删除；清理失败不影响测试结论。"""
    shutil.rmtree(path, ignore_errors=True)
