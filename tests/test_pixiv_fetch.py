from __future__ import annotations

import copy
import json
import socket
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pixivpy3 import PixivError

from pixiv_novel_sync.pixiv_fetch import (
    METADATA_LIMIT, TEXT_LIMIT, WIRE_LIMIT, PixivFetchError,
    deserialize_novel, fetch_novel, serialize_novel, validate_novel,
)
from pixiv_novel_sync.utils_hashing import sha256_text, stable_json_dumps


@pytest.fixture(autouse=True)
def no_real_pixiv(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("tests must use the injected Pixiv client")
    monkeypatch.setattr(socket.socket, "connect", denied)


def novel(novel_id=42):
    return {
        "id": novel_id, "title": "测试作品", "caption": "<p>简介 &amp; 内容</p>",
        "user": {"id": 7, "name": "作者", "account": "fixture",
                 "profile_image_urls": {"medium": "https://i.pximg.net/avatar.jpg"}},
        "image_urls": {"large": "https://i.pximg.net/large.jpg", "medium": "https://i.pximg.net/medium.jpg"},
        "tags": [{"name": "科幻", "translated_name": None}], "series": {"id": 3, "title": "系列"},
        "visible": True, "x_restrict": 0, "text_length": 10, "total_bookmarks": 2,
        "total_view": 3, "create_date": "2026-11-12T12:00:00+09:00", "is_original": True,
        "novel_ai_type": 0,
    }


class Api:
    def __init__(self, metadata=None, text="第一段\r\n\r\n\r\n第二段"):
        self.metadata = novel() if metadata is None else metadata
        self.text = text
        self.calls = []
        self.webview = None

    def novel_detail(self, novel_id):
        self.calls.append(("detail", novel_id))
        return {"novel": self.metadata}

    def webview_novel(self, novel_id):
        self.calls.append(("text", novel_id))
        return self.webview if self.webview is not None else {"id": novel_id, "text": self.text}


def rehash(payload):
    payload["sha256"] = sha256_text(stable_json_dumps({key: value for key, value in payload.items() if key != "sha256"}))
    return stable_json_dumps(payload).encode("utf-8")


def test_fixed_fetch_round_trip_preserves_normalization_and_legacy_hashes():
    api = Api()
    dto = fetch_novel(api, 42)
    assert api.calls == [("detail", 42), ("text", 42)]
    assert dto.text == "第一段\n\n第二段\n"
    assert dto.normalized_metadata()["caption"] == "简介 & 内容"
    assert dto.normalized_metadata()["user_id"] == 7
    expected = {key: value for key, value in api.metadata.items() if key not in {"total_view", "total_bookmarks"}}
    assert dto.meta_hash == sha256_text(stable_json_dumps(expected))
    assert dto.text_hash == sha256_text(dto.text)
    assert deserialize_novel(serialize_novel(dto), expected_novel_id=42) == dto
    assert validate_novel(dto, expected_novel_id=42) is dto


def test_volatile_counts_do_not_force_archive_rewrite():
    api = Api()
    old = fetch_novel(api, 42)
    api.metadata["total_bookmarks"] = 1000
    api.metadata["total_view"] = 2000
    current = fetch_novel(api, 42)
    assert current.meta_hash == old.meta_hash
    assert current.text_hash == old.text_hash
    assert current.normalized_metadata()["total_bookmarks"] == 1000


def test_valid_raw_extensions_are_preserved_without_changing_the_legacy_hash():
    api = Api()
    api.metadata["is_muted"] = False
    api.metadata["marker"] = {"page": 2}
    api.metadata["future_readonly_metadata"] = {"labels": ["original", "long"]}
    dto = fetch_novel(api, 42)
    encoded = serialize_novel(dto)
    assert dto.metadata == api.metadata
    assert "future_readonly_metadata" not in json.loads(encoded)["metadata"]
    expected = {key: value for key, value in api.metadata.items() if key not in {"total_view", "total_bookmarks"}}
    assert dto.meta_hash == sha256_text(stable_json_dumps(expected))
    assert "第一段" not in repr(dto) and "fixture" not in repr(dto)
    assert len(dto.metadata_json.encode("utf-8")) < METADATA_LIMIT


@pytest.mark.parametrize("field", ["access_token", "web_cookie", "command", "callback_url"])
def test_unsafe_raw_extensions_are_rejected_explicitly(field):
    api = Api()
    api.metadata[field] = "private-upstream-secret"
    with pytest.raises(PixivFetchError, match="unsafe_metadata") as raised:
        fetch_novel(api, 42)
    assert "private-upstream-secret" not in str(raised.value)


@pytest.mark.parametrize("field", ["command", "url", "callback_url", "path", "sql"])
def test_wire_rejects_unknown_fields_even_with_a_matching_envelope_hash(field):
    payload = json.loads(serialize_novel(fetch_novel(Api(), 42)))
    payload[field] = "forbidden"
    with pytest.raises(PixivFetchError, match="unexpected_fields"):
        deserialize_novel(rehash(payload), expected_novel_id=42)


@pytest.mark.parametrize("where", ["novel", "user", "tag"])
def test_wire_metadata_is_a_closed_whitelist(where):
    payload = json.loads(serialize_novel(fetch_novel(Api(), 42)))
    value = payload["metadata"] if where == "novel" else payload["raw_metadata"]
    if where == "user":
        value = value["user"]
    elif where == "tag":
        value = value["tags"][0]
    value["secret"] = "forbidden"
    with pytest.raises(PixivFetchError, match="unexpected_fields|unsafe_metadata"):
        deserialize_novel(rehash(payload), expected_novel_id=42)


@pytest.mark.parametrize("field", ["meta_hash", "text_hash", "sha256"])
def test_forged_hash_is_rejected(field):
    payload = json.loads(serialize_novel(fetch_novel(Api(), 42)))
    payload[field] = "0" * 64
    encoded = stable_json_dumps(payload).encode("utf-8") if field == "sha256" else rehash(payload)
    with pytest.raises(PixivFetchError, match="hash_mismatch"):
        deserialize_novel(encoded, expected_novel_id=42)


@pytest.mark.parametrize("field,value", [("title", "not the raw title"), ("user_id", 99),
                                         ("caption", "unrelated caption"), ("tags_json", "[]")])
def test_normalized_metadata_cannot_contradict_the_raw_archive(field, value):
    payload = json.loads(serialize_novel(fetch_novel(Api(), 42)))
    payload["metadata"][field] = value
    with pytest.raises(PixivFetchError, match="metadata_mismatch"):
        deserialize_novel(rehash(payload), expected_novel_id=42)


@pytest.mark.parametrize("asset", [[["inline_image"], "https://i.pximg.net/a.jpg"],
                                  ["inline_image", {"url": "https://i.pximg.net/a.jpg"}]])
def test_malformed_asset_types_are_fixed_protocol_errors(asset):
    payload = json.loads(serialize_novel(fetch_novel(Api(), 42)))
    payload["assets"].append(asset)
    with pytest.raises(PixivFetchError, match="invalid_assets"):
        deserialize_novel(rehash(payload), expected_novel_id=42)


def test_invalid_base64_duplicate_keys_and_nonfinite_json_are_rejected():
    payload = json.loads(serialize_novel(fetch_novel(Api(), 42)))
    payload["text_base64"] = "!" * len(payload["text_base64"])
    with pytest.raises(PixivFetchError, match="invalid_text"):
        deserialize_novel(rehash(payload), expected_novel_id=42)
    for encoded in (b'{"protocol":1,"protocol":1}', b'{"protocol":NaN}'):
        with pytest.raises(PixivFetchError, match="invalid_payload"):
            deserialize_novel(encoded, expected_novel_id=42)


@pytest.mark.parametrize("encoded", [b'{"raw_metadata":' + b'[' * 20 + b'0' + b']' * 20 + b'}',
                                     b'{"assets":[' + b'{},' * 11000 + b'{}]}'],
                         ids=["too_deep", "too_many_containers"])
def test_wire_structure_has_a_budget_before_json_allocates_containers(encoded):
    with pytest.raises(PixivFetchError, match="metadata_too_large"):
        deserialize_novel(encoded, expected_novel_id=42)


@pytest.mark.parametrize("field,value", [("protocol", 2), ("protocol", True), ("kind", "shell")])
def test_wire_version_and_kind_are_fixed(field, value):
    payload = json.loads(serialize_novel(fetch_novel(Api(), 42)))
    payload[field] = value
    with pytest.raises(PixivFetchError, match="unsupported_protocol"):
        deserialize_novel(rehash(payload), expected_novel_id=42)


@pytest.mark.parametrize("novel_id", [True, 0, -1, "https://example.invalid", 2**63])
def test_fetch_only_accepts_a_positive_novel_id(novel_id):
    api = Api()
    with pytest.raises(PixivFetchError, match="invalid_novel_id"):
        fetch_novel(api, novel_id)
    assert api.calls == []


def test_reply_and_deserialized_payload_cannot_cross_novel_identity():
    api = Api(metadata=novel(43))
    with pytest.raises(PixivFetchError, match="novel_id_mismatch"):
        fetch_novel(api, 42)
    assert api.calls == [("detail", 42)]
    api = Api()
    api.webview = {"id": 43, "text": "wrong work"}
    with pytest.raises(PixivFetchError, match="novel_id_mismatch"):
        fetch_novel(api, 42)
    encoded = serialize_novel(fetch_novel(Api(), 42))
    with pytest.raises(PixivFetchError, match="novel_id_mismatch"):
        deserialize_novel(encoded, expected_novel_id=43)


@pytest.mark.parametrize("endpoint", ["detail", "text"])
@pytest.mark.parametrize("error,unavailable", [
    ({"user_message": "尚无此页", "message": ""}, True),
    ({"user_message": "请稍后", "message": "Rate Limit"}, False),
    ({"user_message": "", "message": "invalid token private-secret"}, False),
])
def test_error_pages_never_become_an_archive(endpoint, error, unavailable):
    api = Api()
    response = {"error": error}
    if endpoint == "detail":
        api.novel_detail = lambda novel_id: response
    else:
        api.webview = response
    with pytest.raises(PixivFetchError) as raised:
        fetch_novel(api, 42)
    assert (raised.value.code == "content_unavailable") is unavailable
    assert "private-secret" not in str(raised.value)


def test_pixivpy_hidden_error_body_keeps_unavailable_classification_without_secret_text():
    api = Api()
    def unavailable(novel_id):
        raise PixivError("secret internal failure", body='{"error":{"user_message":"尚无此页","message":""}}')
    api.webview_novel = unavailable
    with pytest.raises(PixivFetchError, match="content_unavailable") as raised:
        fetch_novel(api, 42)
    assert "secret" not in str(raised.value)


@pytest.mark.parametrize("text", ["", " \n\t", {"error": "not text"}])
def test_missing_or_nontext_body_is_not_a_success(text):
    with pytest.raises(PixivFetchError):
        fetch_novel(Api(text=text), 42)


def test_utf8_text_and_metadata_limits_are_checked_before_serialization():
    with pytest.raises(PixivFetchError, match="text_too_large"):
        fetch_novel(Api(text="中" * (TEXT_LIMIT // 3 + 1)), 42)
    metadata = novel()
    metadata["caption"] = "x" * METADATA_LIMIT
    with pytest.raises(PixivFetchError, match="metadata_too_large"):
        fetch_novel(Api(metadata=metadata), 42)


def test_real_maximum_body_has_bounded_round_trip_wire_size():
    dto = fetch_novel(Api(text="x" * (TEXT_LIMIT - 1)), 42)
    encoded = serialize_novel(dto)
    assert dto.text_size == TEXT_LIMIT and len(encoded) <= WIRE_LIMIT
    assert deserialize_novel(encoded, expected_novel_id=42).text_hash == dto.text_hash
    with pytest.raises(PixivFetchError, match="text_too_large"):
        validate_novel(replace(dto, text=dto.text + "x"), expected_novel_id=42)


def test_cover_priority_and_ordered_inline_assets_remain_local_download_inputs():
    api = Api()
    api.webview = {"text": "body", "images": [
        {"url": "https://i.pximg.net/inline.jpg"},
        {"url": "https://i.pximg.net/inline.jpg"},
        {"url": "https://pximg.net.evil.invalid/not-an-asset.jpg"},
    ]}
    dto = fetch_novel(api, 42)
    assert dto.assets == (("cover", "https://i.pximg.net/large.jpg"), ("inline_image", "https://i.pximg.net/inline.jpg"))
    assert dto.normalized_metadata()["cover_url"] == "https://i.pximg.net/large.jpg"


def test_body_that_is_exactly_one_image_url_retains_legacy_asset_collection():
    dto = fetch_novel(Api(text="https://i.pximg.net/inline.jpg"), 42)
    assert ("inline_image", "https://i.pximg.net/inline.jpg") in dto.assets


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://example.invalid/x.jpg", "https://i.pximg.net@evil.invalid/x", "https://i.pximg.net:8080/x"])
def test_cover_cannot_authorize_an_arbitrary_download(url):
    metadata = novel()
    metadata["image_urls"]["large"] = url
    with pytest.raises(PixivFetchError, match="invalid_asset_url"):
        fetch_novel(Api(metadata=metadata), 42)


@pytest.mark.parametrize("cancel_at", ["before", "after_detail", "after_text"])
def test_cancellation_is_checked_at_every_fetch_boundary(cancel_at):
    flag = {"cancel": cancel_at == "before"}
    api = Api()
    detail, text = api.novel_detail, api.webview_novel
    def detail_call(novel_id):
        result = detail(novel_id)
        flag["cancel"] = cancel_at == "after_detail"
        return result
    def text_call(novel_id):
        result = text(novel_id)
        flag["cancel"] = cancel_at == "after_text"
        return result
    api.novel_detail, api.webview_novel = detail_call, text_call
    with pytest.raises(InterruptedError):
        fetch_novel(api, 42, stop_requested=lambda: flag["cancel"])
    assert len(api.calls) == {"before": 0, "after_detail": 1, "after_text": 2}[cancel_at]


def test_missing_detail_novel_uses_only_the_provided_matching_list_item():
    api = Api()
    api.novel_detail = lambda novel_id: SimpleNamespace(novel=None)
    dto = fetch_novel(api, 42, fallback_novel=SimpleNamespace(**copy.deepcopy(novel())))
    assert dto.novel_id == 42
    with pytest.raises(PixivFetchError, match="novel_id_mismatch"):
        fetch_novel(api, 42, fallback_novel=novel(43))
