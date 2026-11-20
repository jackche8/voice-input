"""原子写文件（临时文件 + fsync + rename）：读者只会看到旧内容或完整新内容。证书、组件进程记录、安装任务文件共用。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


__all__ = ["atomic_write_bytes"]
