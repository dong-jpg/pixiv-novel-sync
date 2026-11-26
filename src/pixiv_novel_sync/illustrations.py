from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit


MARKER = re.compile(r"\[(uploadedimage|pixivimage):([0-9]{1,20}(?:-[0-9]{1,10})?)\]")


def collect_mapping(text, webview):
    def field(value, name):
        return value.get(name) if isinstance(value, dict) else getattr(value, name, None)

    def image_url(value):
        if isinstance(value, str):
            return value
        if not isinstance(value, dict):
            return None
        urls = value.get("urls") or value.get("image_urls") or value
        if isinstance(urls, dict):
            for size in ("original", "regular", "large", "medium", "url"):
                if isinstance(urls.get(size), str):
                    return urls[size]
        return None

    mapping = {}
    for kind, identity in dict.fromkeys(MARKER.findall(text)):
        container = field(webview, "images" if kind == "uploadedimage" else "illusts")
        value = container.get(identity) if isinstance(container, dict) else None
        if isinstance(container, dict) and value is None and identity.isdecimal():
            value = container.get(int(identity))
        if isinstance(container, list):
            value = next((item for item in container if str(field(item, "id")) == identity), None)
        url = image_url(value)
        if url:
            mapping[f"{kind}:{identity}"] = url
    return tuple(mapping.items())


def save_mapping(db, novel_id, mapping, *, preserve_existing=False):
    with db.transaction() as connection:
        if not preserve_existing:
            connection.execute("DELETE FROM novel_illustrations WHERE novel_id=?", (novel_id,))
        connection.executemany("INSERT INTO novel_illustrations(novel_id,marker,remote_url) VALUES(?,?,?) "
                               "ON CONFLICT(novel_id,marker) DO UPDATE SET remote_url=excluded.remote_url",
                               [(novel_id, marker, url) for marker, url in mapping])


def mapped_assets(db, novel_id):
    return [dict(row) for row in db.conn.execute(
        """SELECT images.marker,images.remote_url,assets.asset_id,assets.local_path,
                  novels.restrict_value,novels.archive_dir
        FROM novel_illustrations images JOIN novels USING(novel_id)
        LEFT JOIN assets ON assets.novel_id=images.novel_id AND assets.remote_url=images.remote_url
                         AND assets.asset_type='inline_image' AND COALESCE(assets.file_hash,'')!=''
        WHERE images.novel_id=?""", (novel_id,))]


def safe_image_path(settings, asset):
    if not asset or asset.get("asset_id") is None:
        return None
    root = Path(settings.storage.private_dir if asset.get("restrict_value") == "private"
                else settings.storage.public_dir).resolve()
    stored = Path(asset.get("local_path") or "")
    candidates = [Path.cwd() / stored, root / stored]
    if asset.get("archive_dir") and asset.get("remote_url"):
        candidates.append(root / asset["archive_dir"] / "assets" / "inline_image"
                          / Path(urlsplit(asset["remote_url"]).path).name)
    for path in candidates:
        try:
            resolved = path.resolve()
            if resolved.is_relative_to(root) and resolved.is_file() and resolved.stat().st_size > 0:
                return resolved
        except (OSError, ValueError):
            continue
    return None


def image_mime(path):
    try:
        with path.open("rb") as stream:
            header = stream.read(16)
    except OSError:
        return None
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return None


def asset_urls(db, novel_id, *, rescue=False):
    prefix = "/api/rescue/v1" if rescue else "/api/dashboard"
    return {row["marker"]: f"{prefix}/novels/{novel_id}/assets/{row['asset_id']}"
            for row in mapped_assets(db, novel_id) if row["asset_id"] is not None}


def export_images(db, settings, novel_id):
    return {row["marker"]: path for row in mapped_assets(db, novel_id)
            if (path := safe_image_path(settings, row)) is not None}
