from __future__ import annotations

import errno
import os
from pathlib import Path
import shutil
import stat
import tempfile

from ..models import SourceRecord
from ..utils_text import to_markdown


def _kind(path: Path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError("归档路径包含链接，不能自动迁移或修复")
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    if stat.S_ISREG(info.st_mode):
        return "file"
    raise ValueError("归档路径不是常规文件或目录")


def _directory(root, relative):
    if relative.anchor or not relative.parts or ".." in relative.parts:
        raise ValueError("归档目录不在指定书库内")
    target = root
    for part in relative.parts:
        target = target / part
        if _kind(target) not in (None, "directory"):
            raise ValueError("归档目录与文件冲突")
    return target


def _relative(storage, row):
    stored = row["archive_dir"] or storage.relative_novel_dir(row["user_id"], row["author_name"], row["novel_id"], row["title"])
    path = Path(stored)
    return path.relative_to(storage.base_dir(row["restrict_value"]).resolve()) if path.is_absolute() else path


def restore_text_outputs(db, storage, row, write_markdown, write_raw_text):
    if not (write_markdown or write_raw_text):
        return
    directory = _directory(storage.base_dir(row["restrict_value"]).resolve(), _relative(storage, row))
    missing = []
    for enabled, name in ((write_raw_text, "text.txt"), (write_markdown, "text.md")):
        if not enabled:
            continue
        path = directory / name
        if _kind(path) == "file" and path.stat().st_size:
            continue
        if _kind(path.with_suffix(path.suffix + ".tmp")) not in (None, "file"):
            raise ValueError("归档临时文件路径异常")
        missing.append(path)
    if missing:
        text = db.conn.execute("SELECT text_raw FROM novel_texts WHERE novel_id=? AND has_content=1",
                               (row["novel_id"],)).fetchone()
        if text and text[0]:
            for path in missing:
                content = text[0] if path.name == "text.txt" else to_markdown(row["title"], row["author_name"] or "unknown", row["caption"] or "", text[0])
                storage.write_text(path, content)


def _walk(directory):
    def raise_error(error):
        raise error

    for parent, directories, files in os.walk(directory, onerror=raise_error):
        parent = Path(parent)
        for name in directories:
            if _kind(parent / name) != "directory":
                raise ValueError("归档子目录异常")
        for name in files:
            if _kind(parent / name) != "file":
                raise ValueError("归档文件异常")
        yield parent.relative_to(directory), files


def _same_contents(source, target):
    if source.stat().st_size != target.stat().st_size:
        return False
    with source.open("rb") as first, target.open("rb") as second:
        for chunk in iter(lambda: first.read(1024 * 1024), b""):
            if second.read(len(chunk)) != chunk:
                return False
        return not second.read(1)


def _copy_verified(source, target):
    if _kind(target) == "file":
        if _same_contents(source, target):
            return
        raise FileExistsError(f"目标已有不同内容：{target.name}")
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with source.open("rb") as reader, temporary.open("wb") as writer:
            shutil.copyfileobj(reader, writer)
            writer.flush()
            os.fsync(writer.fileno())
        if not _same_contents(source, temporary):
            raise OSError("复制后的归档校验失败")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def _transfer(source, target, check_cancel):
    if not source.exists():
        return
    tree = list(_walk(source))
    check_cancel()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        try:
            source.rename(target)
            return
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
    for relative, files in tree:
        check_cancel()
        dest = _directory(target, relative) if relative.parts else target
        dest.mkdir(parents=True, exist_ok=True)
        for name in files:
            check_cancel()
            _copy_verified(source / relative / name, dest / name)
    check_cancel()
    for relative, files in reversed(tree):
        for name in files:
            (source / relative / name).unlink()
        (source / relative).rmdir()


def reconcile_bookmark_scope(db, storage, novel_id, scope, owner, stop_requested):
    def check_cancel():
        if stop_requested and stop_requested():
            raise InterruptedError("Task stopped by user")

    if scope not in ("public", "private"):
        raise ValueError("未知的收藏范围")
    key = str(novel_id)
    query = """SELECT n.novel_id,n.user_id,n.title,n.restrict_value,n.archive_dir,u.name AS author_name
        FROM novels n LEFT JOIN users u USING(user_id) WHERE novel_id=? AND (restrict_value!=? OR EXISTS
        (SELECT 1 FROM sync_watermarks WHERE sync_type='bookmark_scope_move' AND key=?))"""
    if db.conn.execute(query, (novel_id, scope, key)).fetchone() is None:
        return
    check_cancel()
    db.update_watermark("bookmark_scope_move", {"target": scope}, key)
    with db.transaction() as connection:
        row = connection.execute(query, (novel_id, scope, key)).fetchone()
        if row is None:
            return
        other = "private" if scope == "public" else "public"
        root, old_root = storage.base_dir(scope).resolve(), storage.base_dir(other).resolve()
        if root.is_relative_to(old_root) or old_root.is_relative_to(root):
            raise ValueError("公开和私密归档根目录必须独立")
        relative = _relative(storage, row)
        parts = relative.parts
        if (len(parts) != 4 or parts[0] != "authors" or parts[2] != "novels"
                or parts[3].split("_", 1)[0] != key):
            raise ValueError("不能确认目录属于当前作品，保留归档等待人工核对")
        source, target = _directory(old_root, relative), _directory(root, relative)
        paths = []
        for asset_id, stored in connection.execute("SELECT asset_id,local_path FROM assets WHERE novel_id=?", (novel_id,)):
            path = Path(stored)
            if path.is_absolute():
                parent = next((item for item in (source, target) if path.is_relative_to(item)), None)
                if parent is None:
                    raise ValueError("附件不在作品目录内，保留归档等待人工核对")
                tail = path.relative_to(parent)
            else:
                if path.parts[-(len(parts) + 3):-3] != parts:
                    raise ValueError("无法确认相对附件路径的归属")
                tail = Path(*path.parts[-3:])
            if len(tail.parts) != 3 or tail.parts[0] != "assets" or ".." in tail.parts:
                raise ValueError("附件路径异常")
            paths.append((str(target / tail), asset_id))
        _transfer(source, target, check_cancel)
        check_cancel()
        connection.executemany("UPDATE assets SET local_path=? WHERE asset_id=?", paths)
        connection.execute("UPDATE novels SET restrict_value=?,archive_dir=? WHERE novel_id=?", (scope, relative.as_posix(), novel_id))
        owner_key = str(owner if owner is not None else row["user_id"])
        connection.execute("DELETE FROM sources WHERE novel_id=? AND source_type=? AND source_key IN (?,?)",
                           (novel_id, f"bookmark_{other}", owner_key, str(row["user_id"])))
        db.upsert_source(SourceRecord(novel_id, f"bookmark_{scope}", owner_key))
        db.refresh_rescue_item("novel", novel_id)
        connection.execute("DELETE FROM sync_watermarks WHERE sync_type='bookmark_scope_move' AND key=?", (key,))
