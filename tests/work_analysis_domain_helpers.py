import hashlib

from pixiv_novel_sync.work_analysis.contracts import fingerprint
from pixiv_novel_sync.work_analysis.manifest import public_preview_identity, validate_manifest


def source_fixture(bodies):
    chapters, texts = [], {}
    for index, body in enumerate(bodies, 1):
        texts[index] = body
        chapters.append({
            "novel_id": index, "position": index - 1, "title": f"Chapter {index}",
            "text_hash": hashlib.sha256(body.encode()).hexdigest(), "chars": len(body),
            "utf8_bytes": len(body.encode()), "fetched_at": "2026-01-01",
            "has_text": bool(body.strip()),
        })
    manifest = {
        "schema_version": 1, "source_kind": "series", "source_id": 10,
        "title": "Synthetic work", "publication_state": "unknown", "publication_source": "unknown",
        "order_source": "user_confirmed", "expected_count": len(chapters),
        "expected_count_observed_at": "2026-01-01", "remote_inventory_verified": False,
        "requires_partial_confirmation": True, "chapters": chapters,
    }
    manifest["manifest_fingerprint"] = fingerprint(public_preview_identity(manifest))
    return validate_manifest(manifest), texts


def capacity_preflight(maximum_chars):
    def check(payload):
        size = sum(len(item["content"]) for item in payload["messages"])
        fits = size <= maximum_chars
        return {
            "version": 2, "can_run": fits, "work_can_segment": not fits,
            "eligible": [{"provider_id": 1, "model_key": "synthetic"}] if fits else [],
            "excluded": [] if fits else [{"reason": "context_overflow"}],
            "excluded_truncated": False,
        }
    return check
