"""Regression coverage for cache identity, freshness and API completeness."""

import json
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from codecks_cli import CliError, config
from codecks_cli.mcp_server import _core, _tools_read
from codecks_cli.store import CardStore


def snapshot(age=0):
    return {
        "scope": dict(_core._cache_scope),
        "fetched_at": (datetime.now(UTC) - timedelta(seconds=age)).isoformat(),
        "cards_result": {"cards": [{"id": "old", "title": "Cached title"}]},
        "account": {},
    }


@pytest.mark.parametrize("age", [600, -60])
def test_disk_rejects_expired_and_future_snapshots(tmp_path, monkeypatch, age):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps(snapshot(age)))
    monkeypatch.setattr(_core, "CACHE_PATH", str(path))
    assert not _core._load_cache_from_disk()
    assert _core._snapshot_cache is None


@pytest.mark.parametrize("field", ["account_id", "credential", "account_header", None])
def test_disk_rejects_foreign_or_unscoped_snapshots(tmp_path, monkeypatch, field):
    data = snapshot()
    if field is None:
        del data["scope"]
    else:
        data["scope"][field] = "other"
    path = tmp_path / "cache.json"
    path.write_text(json.dumps(data))
    monkeypatch.setattr(_core, "CACHE_PATH", str(path))
    assert not _core._load_cache_from_disk()


def test_disk_load_preserves_age(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps(snapshot(30)))
    monkeypatch.setattr(_core, "CACHE_PATH", str(path))
    assert _core._load_cache_from_disk()
    assert 29 <= time.monotonic() - _core._cache_loaded_at <= 32


def test_changed_credentials_verify_account_and_discard_old_data(monkeypatch):
    monkeypatch.setattr(_core, "_snapshot_cache", snapshot())
    config._cache["decks"] = {"old": {}}
    monkeypatch.setattr(config, "SESSION_TOKEN", "another-token")
    client = MagicMock()
    client.get_account.return_value = {"account": {"new-account": {"name": "New"}}}
    monkeypatch.setattr(_core, "_get_client", lambda: client)
    scope = _core._ensure_cache_scope()
    assert scope["account_id"] == "new-account"
    assert _core._snapshot_cache is None
    assert config._cache == {}
    _core._ensure_cache_scope()
    client.get_account.assert_called_once()


@pytest.mark.parametrize("method", ["mark_done", "unknown_mutation"])
def test_mutation_cannot_resurrect_disk_cards(tmp_path, monkeypatch, method):
    path = tmp_path / "cache.json"
    monkeypatch.setattr(_core, "CACHE_PATH", str(path))
    data = snapshot(30)
    monkeypatch.setattr(_core, "_snapshot_cache", data)
    monkeypatch.setattr(_core, "_cache_loaded_at", time.monotonic() - 30)
    _core._repo.load(data["cards_result"]["cards"])
    _core._persist_cache_to_disk()
    _core._write_through_cache(method, {})
    disk = json.loads(path.read_text())
    assert "cards_result" not in disk
    if method == "mark_done":
        assert disk["fetched_at"] == data["fetched_at"]
    _core._invalidate_cache()
    _core._load_cache_from_disk()
    assert _core._repo.get("old") is None
    assert _tools_read._try_cache("cards_result") is None


def test_body_search_uses_api_when_snapshot_has_only_titles(monkeypatch):
    monkeypatch.setattr(_core, "_snapshot_cache", snapshot())
    monkeypatch.setattr(_core, "_cache_loaded_at", time.monotonic())
    call = MagicMock(return_value={"cards": [{"id": "body-match", "title": "Unrelated"}]})
    monkeypatch.setattr(_tools_read, "_call", call)
    result = _tools_read.list_cards(search="body-only phrase")
    assert result["cards"][0]["id"] == "body-match"
    assert call.call_args.kwargs["include_content"] is True


def test_get_card_content_and_expired_metadata_use_api(monkeypatch):
    card_id = "aaaabbbb-cccc-dddd-eeee-ffffffffffff"
    data = snapshot()
    data["cards_result"]["cards"][0]["id"] = card_id
    monkeypatch.setattr(_core, "_snapshot_cache", data)
    monkeypatch.setattr(_core, "_cache_loaded_at", time.monotonic())
    _core._repo.load(data["cards_result"]["cards"])
    call = MagicMock(return_value={"id": card_id, "title": "Live", "content": "Live body"})
    monkeypatch.setattr(_tools_read, "_call", call)
    assert "Live body" in _tools_read.get_card(card_id, include_content=True)["content"]
    monkeypatch.setattr(_core, "_cache_loaded_at", time.monotonic() - 600)
    assert (
        "Live"
        in _tools_read.get_card(card_id, include_content=False, include_conversations=False)[
            "title"
        ]
    )
    assert call.call_count == 2


def test_full_store_refresh_removes_absent_cards_and_decks():
    store = CardStore(":memory:")
    try:
        store.upsert_cards([{"id": "gone", "title": "Old"}])
        store.upsert_decks([{"id": "gone-deck", "title": "Old"}])
        store.upsert_cards([{"id": "new", "title": "New"}], replace=True)
        store.upsert_decks([], replace=True)
        assert store.get_card("gone") is None
        assert store.get_card("new")["title"] == "New"
        assert store._conn.execute("SELECT count(*) FROM decks").fetchone()[0] == 0
        store.upsert_cards([], replace=True)
        assert store.get_card("new") is None
    finally:
        store.close()


def test_refresh_overlapping_mutation_does_not_publish(monkeypatch):
    client = MagicMock()
    client.get_account.return_value = {}
    client.list_hand.return_value = []
    client.list_decks.return_value = []

    def fetch_cards(**kwargs):
        _core._invalidate_cache_for("mark_done")
        return {"cards": [{"id": "stale", "title": "Before mutation"}]}

    client.list_cards.side_effect = fetch_cards
    monkeypatch.setattr(_core, "_get_client", lambda: client)
    with pytest.raises(CliError, match="overlapped"):
        _core._warm_cache_impl()
    assert _core._snapshot_cache is None


def test_card_mutation_invalidates_hand_metadata_and_hand_filter(monkeypatch):
    data = snapshot()
    data["hand"] = [{"id": "old", "status": "started"}]
    monkeypatch.setattr(_core, "_snapshot_cache", data)
    monkeypatch.setattr(_core, "_cache_loaded_at", time.monotonic())
    _core._invalidate_cache_for("mark_done")
    assert "hand" not in data
    # A hand-only read cannot filter cards using a missing hand snapshot.
    data["cards_result"] = {"cards": []}
    call = MagicMock(return_value={"cards": []})
    monkeypatch.setattr(_tools_read, "_call", call)
    _tools_read.list_cards(hand_only=True)
    call.assert_called_once()
