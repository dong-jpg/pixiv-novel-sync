"""Fixed, spawn-based renderer. Arguments are paths chosen by the parent."""
from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
import threading

from .contracts import (
    COVER_LIMIT, DIAGNOSTIC_LIMIT, KINDS, MANIFEST_LIMIT, RESULT_LIMIT,
    TEXT_LIMIT, WORKER_ERRORS, RemoteError, canonical_bytes,
)


def _local_file(path: Path, root: Path, *, exists: bool = True) -> Path:
    path = Path(path)
    if (path.parent != root or path.is_symlink() or path.resolve() != path.absolute()
            or (exists and not path.is_file())):
        raise RemoteError("input_mismatch", 400)
    return path


def _read(path: Path, limit: int) -> bytes:
    with path.open("rb") as source:
        value = source.read(limit + 1)
    if len(value) > limit:
        raise RemoteError("input_mismatch", 413)
    return value


def run_child(kind, manifest_path, text_path, cover_path, output_path) -> None:
    """Render one allowlisted task without importing the app or its database."""
    if not isinstance(kind, str) or kind not in KINDS:
        raise RemoteError("unsupported_kind", 400)
    from .epub_task import decode_manifest, render_epub

    output_path = Path(output_path)
    root = output_path.parent
    if not root.is_absolute() or root.is_symlink() or root.resolve() != root.absolute():
        raise RemoteError("input_mismatch", 400)
    _local_file(output_path, root, exists=False)
    manifest = decode_manifest(_read(_local_file(Path(manifest_path), root), MANIFEST_LIMIT), kind)
    if kind == "diagnostic.v1":
        if text_path is not None or cover_path is not None:
            raise RemoteError("input_mismatch", 400)
        result = canonical_bytes(manifest)
        limit = DIAGNOSTIC_LIMIT
    else:
        if text_path is None:
            raise RemoteError("input_mismatch", 400)
        text = _read(_local_file(Path(text_path), root), TEXT_LIMIT)
        cover = _read(_local_file(Path(cover_path), root), COVER_LIMIT) if cover_path is not None else None
        result = render_epub(manifest, text, cover, root)
        limit = RESULT_LIMIT
    if len(result) > limit:
        raise RemoteError("result_too_large", 413)
    # No caller-selected archive member is ever extracted to disk.
    with output_path.open("xb") as target:
        target.write(result)
        target.flush()
        os.fsync(target.fileno())


def _child_entry(kind, manifest_path, text_path, cover_path, output_path) -> None:
    # spawn passes only the fixed task vocabulary and internal file paths.
    # Discard bootstrap keys and any accidentally inherited business config
    # before importing or invoking the renderer.
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "TZ"}
    keep = {name: value for name, value in os.environ.items() if name.upper() in allowed}
    os.environ.clear()
    os.environ.update(keep)
    os.umask(0o077)
    try:
        run_child(kind, manifest_path, text_path, cover_path, output_path)
    except Exception as exc:
        code = exc.code if isinstance(exc, RemoteError) and exc.code in WORKER_ERRORS else "render_failed"
        try:
            Path(output_path).with_suffix(".error").write_text(code, encoding="ascii")
        except OSError:
            pass
        # Do not emit exception tracebacks containing input text or local paths.
        raise SystemExit(1) from None


class ChildHandle:
    def __init__(self, process):
        self._process = process
        self._lock = threading.RLock()

    def poll(self) -> int | None:
        with self._lock:
            if self._process.is_alive():
                return None
            self._process.join(timeout=0)
            return self._process.exitcode

    def stop(self) -> None:
        with self._lock:
            if self._process.is_alive():
                self._process.terminate()
            self._process.join(timeout=5)
            if self._process.is_alive():
                self._process.kill()
                self._process.join(timeout=5)


class SpawnRunner:
    def start(self, kind, manifest_path, text_path, cover_path, output_path) -> ChildHandle:
        process = multiprocessing.get_context("spawn").Process(
            target=_child_entry,
            args=(kind, manifest_path, text_path, cover_path, output_path),
            name="pixiv-remote-renderer",
            daemon=True,
        )
        process.start()
        return ChildHandle(process)
