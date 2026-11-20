"""Fixed, spawn-based renderer. Arguments are paths chosen by the parent."""
from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
import threading
import time

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


def run_child(kind, manifest_path, text_path, cover_path, output_path, *, credentials=None, permit=None) -> None:
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
    elif kind in {'checkin.status.v1','checkin.submit.v1'}:
        from .checkin_task import execute
        if text_path is not None or cover_path is not None:
            raise RemoteError('input_mismatch',400)
        result=execute(manifest,credentials,permit)
        limit=DIAGNOSTIC_LIMIT
    elif kind=='ai.text.v1':
        from .ai_task import validate_result
        if text_path is not None or cover_path is not None or credentials is not None:
            raise RemoteError('input_mismatch',400)
        result=canonical_bytes(permit(1))
        validate_result(result)
        limit=RESULT_LIMIT
    elif kind in {'pixiv.novel.v1','pixiv.api.v1'}:
        if text_path is not None or cover_path is not None:
            raise RemoteError('input_mismatch',400)
        if credentials is not None:
            raise RemoteError('input_mismatch',400)
        if kind=='pixiv.novel.v1':
            from ..pixiv_fetch import fetch_novel,serialize_novel
            class ScopedAPI:
                def novel_detail(self,novel_id):
                    if novel_id!=manifest['novel_id']: raise RemoteError('input_mismatch',400)
                    return permit(1)
                def webview_novel(self,novel_id):
                    if novel_id!=manifest['novel_id']: raise RemoteError('input_mismatch',400)
                    return permit(2)
            result=serialize_novel(fetch_novel(ScopedAPI(),manifest['novel_id']))
        else:
            from .pixiv_calls import plain_result
            result=canonical_bytes(plain_result(permit(1)))
        limit=RESULT_LIMIT
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


def _child_entry(kind, manifest_path, text_path, cover_path, output_path, credentials=None, pipe=None) -> None:
    # spawn passes only the fixed task vocabulary and internal file paths.
    # Discard bootstrap keys and any accidentally inherited business config
    # before importing or invoking the renderer.
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "TZ"}
    keep = {name: value for name, value in os.environ.items() if name.upper() in allowed}
    os.environ.clear()
    os.environ.update(keep)
    os.umask(0o077)
    def permit(ordinal):
        while True:
            pipe.send(ordinal)
            if not pipe.poll(20):
                raise RemoteError('worker_transient',503)
            reply=pipe.recv()
            if not isinstance(reply,dict) or 'error' in reply:
                raise RemoteError('worker_transient',503)
            if reply.get('state')=='ready':
                return reply['data']
            if reply.get('allowed') is True:
                return
            if reply.get('state')=='pending':
                time.sleep(1)
                continue
            wait=reply.get('retry_after')
            if type(wait) not in (int,float) or not 0<wait<=30:
                raise RemoteError('protocol_mismatch',502)
            time.sleep(min(wait,2))
    try:
        if kind in {'pixiv.novel.v1','pixiv.api.v1','ai.text.v1','checkin.status.v1','checkin.submit.v1'}:
            run_child(kind, manifest_path, text_path, cover_path, output_path, credentials=credentials, permit=permit)
        else:
            run_child(kind, manifest_path, text_path, cover_path, output_path)
    except Exception as exc:
        code = exc.code if isinstance(exc, RemoteError) and exc.code in WORKER_ERRORS else "render_failed"
        try:
            Path(output_path).with_suffix(".error").write_text(code, encoding="ascii")
        except OSError:
            pass
        # Do not emit exception tracebacks containing input text or local paths.
        raise SystemExit(1) from None
    finally:
        if pipe is not None:
            pipe.close()


class ChildHandle:
    def __init__(self, process, pipe=None, permit=None):
        self._process = process
        self._lock = threading.RLock()
        self._pipe, self._permit = pipe, permit

    def poll(self) -> int | None:
        # Keep network callbacks outside the process-control lock so the lease
        # watchdog can always terminate the child.
        with self._lock:
            pipe=self._pipe
            try: ordinal=pipe.recv() if pipe is not None and pipe.poll() else None
            except (EOFError,OSError): ordinal=None
        if ordinal is not None:
            try:
                if type(ordinal) is not int or ordinal not in (1,2):
                    raise RemoteError('invalid_request',400)
                reply=self._permit(ordinal)
            except Exception:
                reply={'error':'request_denied'}
            with self._lock:
                if pipe is self._pipe:
                    try: pipe.send(reply)
                    except (BrokenPipeError,EOFError,OSError): pass
        with self._lock:
            if self._process.is_alive():
                return None
            self._process.join(timeout=0)
            if self._pipe is not None:
                self._pipe.close()
                self._pipe=None
            return self._process.exitcode

    def stop(self) -> None:
        with self._lock:
            if self._process.is_alive():
                self._process.terminate()
            self._process.join(timeout=5)
            if self._process.is_alive():
                self._process.kill()
                self._process.join(timeout=5)
            if self._pipe is not None:
                self._pipe.close()
                self._pipe=None


class SpawnRunner:
    def start(self, kind, manifest_path, text_path, cover_path, output_path, *, credentials=None, permit=None) -> ChildHandle:
        context=multiprocessing.get_context('spawn')
        parent, child = context.Pipe() if kind in {'pixiv.novel.v1','pixiv.api.v1','ai.text.v1','checkin.status.v1','checkin.submit.v1'} else (None,None)
        process = context.Process(
            target=_child_entry,
            args=(kind, manifest_path, text_path, cover_path, output_path, credentials, child),
            name="pixiv-remote-renderer",
            daemon=True,
        )
        process.start()
        if child is not None: child.close()
        return ChildHandle(process,parent,permit)
