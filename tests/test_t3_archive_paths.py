from __future__ import annotations

import pytest

from pixiv_novel_sync.storage_files import FileStorage
from pixiv_novel_sync.web.utils import _remove_archive_files
from pixiv_novel_sync.webapp import _ArchiveTrash
from test_archive_integrity import make_settings


def test_collect_archive_paths_preserves_stored_locations_and_legacy_assets(tmp_path):
    settings = make_settings(tmp_path)
    storage = FileStorage(settings)
    old_dir = settings.storage.public_dir / "legacy"
    asset = old_dir / "assets" / "cover" / "image.jpg"
    outside = tmp_path / "outside.txt"
    refs = [
        {"novel_id": "invalid", "asset_paths": [str(outside)]},
        {"novel_id": 9, "user_id": "invalid"},
        {"novel_id": 0, "asset_paths": [str(outside)]},
        {
            "novel_id": "12", "user_id": "3", "restrict_value": "private",
            "archive_dir": "authors/original/novels/12",
            "author_name": "renamed", "title": "renamed",
            "asset_paths": [str(asset), "", str(outside), str(asset)],
        },
        {"novel_id": 13},
        {"novel_id": 14, "archive_dir": str(old_dir)},
    ]
    dirs, assets = storage._collect_archive_paths(refs)
    assert dirs == [
        settings.storage.private_dir / "authors/original/novels/12",
        old_dir, old_dir,
        storage.novel_dir("public", 0, "unknown", 13, "novel_13"),
        old_dir,
    ]
    assert assets == [asset, outside, asset]
    assert not settings.storage.public_dir.exists()  # Collection has no filesystem side effects.


@pytest.mark.parametrize("mode", ["remove", "trash"])
def test_archive_consumers_share_collector_and_keep_storage_boundary(tmp_path, monkeypatch, mode):
    settings = make_settings(tmp_path)
    storage = FileStorage(settings)
    novel_dir = settings.storage.public_dir / "original"
    text = novel_dir / "text.txt"
    cover = novel_dir / "assets" / "cover" / "image.jpg"
    loose_asset = settings.storage.private_dir / "loose.jpg"
    outside = tmp_path / "outside"
    outside_text = outside / "keep.txt"
    for path in [text, cover, loose_asset, outside_text]:
        storage.write_text(path, path.name)
    refs = [
        {"novel_id": 1, "archive_dir": "original",
         "asset_paths": [str(cover), str(loose_asset), str(outside_text)]},
        {"novel_id": 1, "archive_dir": "original"},
        {"novel_id": 2, "archive_dir": str(outside)},
        {"novel_id": 3, "archive_dir": "../outside"},
        {"novel_id": 4, "archive_dir": "missing"},
    ]
    calls = []
    collect = FileStorage._collect_archive_paths

    def tracked(self, archive_refs):
        calls.append(archive_refs)
        return collect(self, archive_refs)

    monkeypatch.setattr(FileStorage, "_collect_archive_paths", tracked)
    if mode == "remove":
        stats = _remove_archive_files(settings, refs)
        trash = None
    else:
        trash = _ArchiveTrash(settings, refs)
        trash.stage()
        stats = trash.stats
    assert calls == [refs]
    assert stats == {"dirs_removed": 1, "files_removed": 1, "missing": 1, "skipped": 2}
    assert not novel_dir.exists()
    assert not loose_asset.exists()
    assert outside_text.read_text(encoding="utf-8") == "keep.txt"
    if trash is not None:
        trash.rollback()
        assert text.read_text(encoding="utf-8") == "text.txt"
        assert cover.read_text(encoding="utf-8") == "image.jpg"
        assert loose_asset.read_text(encoding="utf-8") == "loose.jpg"


@pytest.mark.parametrize("mode", ["remove", "trash"])
def test_archive_consumers_reject_symlink_escape(tmp_path, mode):
    settings = make_settings(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    link = settings.storage.public_dir / "link"
    link.parent.mkdir()
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Directory symlinks unavailable: {exc}")
    refs = [{"novel_id": 1, "archive_dir": str(link), "asset_paths": [str(link / "keep.txt")]}]
    if mode == "remove":
        stats = _remove_archive_files(settings, refs)
    else:
        trash = _ArchiveTrash(settings, refs)
        trash.stage()
        stats = trash.stats
        trash.rollback()
    assert stats == {"dirs_removed": 0, "files_removed": 0, "missing": 0, "skipped": 2}
    assert keep.read_text(encoding="utf-8") == "keep"
    assert link.is_symlink()
