from __future__ import annotations

import os
import secrets
from pathlib import Path


def secure_atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    """把完整字节内容安全地原子替换到目标路径。"""
    target = Path(path)
    temporary = target.with_name(f"{target.name}.{secrets.token_hex(16)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    fd: int | None = None
    created = False

    try:
        fd = os.open(temporary, flags, mode)
        created = True
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError("os.write 未写入任何字节")
            offset += written
        os.fsync(fd)
        closing_fd = fd
        fd = None
        os.close(closing_fd)
        os.replace(temporary, target)
        created = False
        os.chmod(target, mode)
    except BaseException:
        if fd is not None:
            closing_fd = fd
            fd = None
            try:
                os.close(closing_fd)
            except OSError:
                pass
        if created:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        raise
