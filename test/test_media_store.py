"""ImageRef、媒体存储、compact 视图与旧 history 迁移测试。"""

from __future__ import annotations

import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent.compaction import estimate_message_tokens, run_local_compaction
from agent.compaction_view import build_compaction_view
from agent.media_store import MediaBlobStore, migrate_legacy_data_uri_messages
from core.conversation_history import ConversationHistory
from core.message import Message, MessageRole


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00\x05"
    b"\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _image_message(ref) -> Message:
    return Message(
        role=MessageRole.USER,
        content=[
            {"type": "text", "text": "请查看图片"},
            {"type": "image_ref", "image_ref": ref.to_dict()},
        ],
    )


def test_image_ref_round_trip_is_independent_from_source_file() -> None:
    """源文件删除或覆盖后，同一 ImageRef 仍展开为完全相同的 provider 内容。"""

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        source = root / "screen.png"
        source.write_bytes(PNG_BYTES)
        store = MediaBlobStore(root / "media")
        ref = store.put_file(source, source_kind="direct")
        message = _image_message(ref)

        logical = json.dumps(message.to_dict(), ensure_ascii=False)
        first = message.to_provider_dict(store)
        source.write_bytes(b"overwritten")
        source.unlink()
        restarted_store = MediaBlobStore(root / "media")
        second = message.to_provider_dict(restarted_store)

        assert "image_ref" in logical
        assert "data:image" not in logical
        assert first == second
        assert first["content"][1]["image_url"]["url"].startswith(
            "data:image/png;base64,"
        )


def test_provider_serialization_rejects_image_ref_without_media_store() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = MediaBlobStore(Path(td) / "media")
        message = _image_message(store.put_bytes(PNG_BYTES, mime_type="image/png"))
        history = ConversationHistory([message])

        assert history.logical_messages()[0]["content"][1]["type"] == "image_ref"
        with pytest.raises(ValueError, match="MediaBlobStore"):
            history.provider_messages()


def test_media_store_deduplicates_concurrent_writes() -> None:
    """并发摄取同一内容只能形成一个 blob，且不能残留临时文件。"""

    with tempfile.TemporaryDirectory() as td:
        store = MediaBlobStore(Path(td) / "media")

        def put_once(_index: int):
            return store.put_bytes(PNG_BYTES, mime_type="image/png")

        with ThreadPoolExecutor(max_workers=8) as pool:
            refs = list(pool.map(put_once, range(32)))

        assert len({ref.blob_id for ref in refs}) == 1
        blob_files = [
            path for path in (Path(td) / "media" / "blobs").rglob("*")
            if path.is_file()
        ]
        assert len(blob_files) == 1
        assert not list((Path(td) / "media").rglob("*.tmp"))


def test_media_store_rejects_corrupted_blob() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = MediaBlobStore(Path(td) / "media")
        ref = store.put_bytes(PNG_BYTES, mime_type="image/png")
        store._blob_path(ref.sha256).write_bytes(b"corrupt")
        with pytest.raises(ValueError, match="校验失败|长度不匹配"):
            store.to_data_uri(ref)


def test_media_store_rejects_missing_blob_during_history_validation() -> None:
    with tempfile.TemporaryDirectory() as td:
        store = MediaBlobStore(Path(td) / "media")
        ref = store.put_bytes(PNG_BYTES, mime_type="image/png")
        message = _image_message(ref)
        store._blob_path(ref.sha256).unlink()

        with pytest.raises(ValueError, match="不存在或不可读"):
            store.validate_messages([message])


def test_legacy_data_uri_migration_preserves_provider_bytes() -> None:
    """旧 data URI 迁移只改变逻辑表示，provider 看到的图片正文保持一致。"""

    with tempfile.TemporaryDirectory() as td:
        store = MediaBlobStore(Path(td) / "media")
        ref = store.put_bytes(PNG_BYTES, mime_type="image/png")
        data_uri = store.to_data_uri(ref)
        legacy = Message(
            role=MessageRole.USER,
            content=[{
                "type": "image_url",
                "image_url": {"url": data_uri, "detail": "auto"},
            }],
        )

        replacement, count = migrate_legacy_data_uri_messages([legacy], store)
        logical = replacement[0].to_dict()
        provider = replacement[0].to_provider_dict(store)

        assert count == 1
        assert logical["content"][0]["type"] == "image_ref"
        assert data_uri not in json.dumps(logical)
        assert provider == legacy.to_dict()


def test_compaction_view_omits_images_but_retained_message_stays_restorable() -> None:
    """摘要派生视图不带图，原始 Message 仍保留 ImageRef 和视觉预算。"""

    with tempfile.TemporaryDirectory() as td:
        store = MediaBlobStore(Path(td) / "media")
        ref = store.put_bytes(PNG_BYTES, mime_type="image/png", file_name="screen.png")
        message = _image_message(ref)

        view = build_compaction_view([message])
        view_dump = json.dumps(view, ensure_ascii=False)
        logical_dump = json.dumps(message.to_dict(), ensure_ascii=False)

        assert "sha256=" in view_dump
        assert "image_ref" not in view_dump
        assert "data:image" not in view_dump
        assert "image_ref" in logical_dump
        assert message.to_provider_dict(store)["content"][1]["type"] == "image_url"
        assert estimate_message_tokens([message]) >= ref.visual_tokens


def test_compaction_request_uses_image_manifest_instead_of_protocol_image_part() -> None:
    """run_local_compaction 发出的 provider 请求不得含自定义 image_ref 或 base64。"""

    with tempfile.TemporaryDirectory() as td:
        store = MediaBlobStore(Path(td) / "media")
        ref = store.put_bytes(PNG_BYTES, mime_type="image/png", file_name="screen.png")
        calls = []

        class Completions:
            def create(self, **kwargs):
                calls.append(kwargs)
                message = type("Summary", (), {"content": "保留图片事实", "tool_calls": None})()
                choice = type("Choice", (), {"message": message})()
                return type("Response", (), {"choices": [choice], "usage": None})()

        client = type(
            "Client",
            (),
            {"chat": type("Chat", (), {"completions": Completions()})()},
        )()
        llm = type(
            "LLM",
            (),
            {
                "model": "fake",
                "client": client,
                "max_output_tokens": 1024,
                "output_token_param": "none",
            },
        )()

        run_local_compaction(
            llm=llm,
            system_message=None,
            history=[_image_message(ref)],
            hard_limit_tokens=100_000,
            estimate_request_tokens=lambda _messages: 100,
        )

        request_dump = json.dumps(calls[0]["messages"], ensure_ascii=False)
        assert "sha256=" in request_dump
        assert "image_ref" not in request_dump
        assert "data:image" not in request_dump
