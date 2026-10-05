"""Tests for admin.py dispatch operations (deck creation, cache seeding)."""

from unittest.mock import patch

import pytest

from codecks_cli import config
from codecks_cli.admin import create_deck, update_deck, update_milestone
from codecks_cli.exceptions import CliError


def test_update_deck_dispatch_and_invalidation():
    deck_id = "b939bc6f-ab08-11f1-a63e-3b5174846072"
    fields = {
        "title": "Renamed",
        "description": "",
        "deckType": "task",
        "defaultCard": {"effort": 0, "assigneeId": None, "masterTags": ["test"]},
    }
    config._cache["decks"] = {"old": True}
    with (
        patch("codecks_cli.admin.api.dispatch", return_value={}) as dispatch,
        patch(
            "codecks_cli.admin.api.query",
            return_value={"deck": {deck_id: {"default_card": fields["defaultCard"]}}},
        ),
    ):
        assert update_deck(deck_id, fields)["ok"]
        dispatch.assert_called_once_with("decks/update", {"id": deck_id, **fields})
    assert "decks" not in config._cache
    with patch("codecks_cli.admin.api.dispatch", side_effect=CliError("failed")):
        config._cache["decks"] = {}
        with pytest.raises(CliError):
            update_deck(deck_id, {"defaultCard": {}})
        assert "decks" not in config._cache


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"deck": {}},
        {"deck": {"00000000-0000-0000-0000-000000000001": {}}},
        {"deck": {"00000000-0000-0000-0000-000000000001": {"defaultCard": {"content": "old"}}}},
        CliError("read failed", error_code="NETWORK_ERROR", retryable=True),
    ],
)
def test_update_deck_does_not_acknowledge_unverified_template(response):
    deck_id = "00000000-0000-0000-0000-000000000001"
    with (
        patch("codecks_cli.admin.api.dispatch", return_value={}),
        patch("codecks_cli.admin.api.query") as query,
    ):
        if isinstance(response, Exception):
            query.side_effect = response
        else:
            query.return_value = response
        config._cache["decks"] = {}
        with pytest.raises(CliError) as error:
            update_deck(deck_id, {"defaultCard": {"content": "new"}})
        assert error.value.error_code == "DECK_UPDATE_UNVERIFIED"
        assert error.value.retryable is False
        assert "decks" not in config._cache


def test_update_deck_verifies_clear_but_metadata_only_needs_no_read():
    deck_id = "00000000-0000-0000-0000-000000000001"
    with (
        patch("codecks_cli.admin.api.dispatch", return_value={}),
        patch(
            "codecks_cli.admin.api.query", return_value={"deck": {deck_id: {"defaultCard": {}}}}
        ) as query,
    ):
        assert update_deck(deck_id, {"defaultCard": {}})["ok"]
        query.assert_called_once_with(
            {'deck(["00000000-0000-0000-0000-000000000001"])': ["defaultCard"]}
        )
        query.reset_mock()
        assert update_deck(deck_id, {"title": "new"})["ok"]
        query.assert_not_called()


@pytest.mark.parametrize(
    "deck_id, fields",
    [
        ("invalid", {"title": "A"}),
        ("z" * 36, {"title": "A"}),
        ("b939bc6f-ab08-11f1-a63e-3b5174846072", {}),
        ("b939bc6f-ab08-11f1-a63e-3b5174846072", {"id": "other"}),
        ("b939bc6f-ab08-11f1-a63e-3b5174846072", {"title": " "}),
        ("b939bc6f-ab08-11f1-a63e-3b5174846072", {"description": None}),
        ("b939bc6f-ab08-11f1-a63e-3b5174846072", {"deckType": []}),
        ("b939bc6f-ab08-11f1-a63e-3b5174846072", {"defaultCard": {"effort": True}}),
        ("b939bc6f-ab08-11f1-a63e-3b5174846072", {"defaultCard": {"priority": "high"}}),
        ("b939bc6f-ab08-11f1-a63e-3b5174846072", {"defaultCard": {"assigneeId": "bad"}}),
        ("b939bc6f-ab08-11f1-a63e-3b5174846072", {"defaultCard": {"content": None}}),
        ("b939bc6f-ab08-11f1-a63e-3b5174846072", {"defaultCard": {"masterTags": [None]}}),
        ("b939bc6f-ab08-11f1-a63e-3b5174846072", {"defaultCard": {"status": "done"}}),
    ],
)
def test_update_deck_rejects_invalid_fields_before_dispatch(deck_id, fields):
    with patch("codecks_cli.admin.api.dispatch") as dispatch:
        with pytest.raises(CliError) as error:
            update_deck(deck_id, fields)
        assert error.value.error_code == "INVALID_INPUT"
        dispatch.assert_not_called()


_PID = "project-1"


@pytest.mark.parametrize(
    "fields",
    [
        {
            "name": "Renamed",
            "description": "",
            "color": "brown",
            "date": "2028-02-29",
            "startDate": None,
        },
        {"startDate": "2026-10-05"},
        *[{"color": c} for c in ("gray", "brown", "yellow", "red", "pink", "blue", "green")],
    ],
)
def test_update_milestone_verified_fields_and_cache(fields):
    mid = "00000000-0000-0000-0000-000000000001"
    returned = {("start_date" if k == "startDate" else k): v for k, v in fields.items()}
    config._cache["milestone_records"] = {mid: {"name": "Old"}}
    with (
        patch("codecks_cli.admin.api.dispatch", return_value={}) as dispatch,
        patch("codecks_cli.admin.api.query", return_value={"milestone": {mid: returned}}),
    ):
        assert update_milestone(mid, fields)["ok"]
        dispatch.assert_called_once_with("milestones/update", {"id": mid, **fields})
    assert "milestone_records" not in config._cache


@pytest.mark.parametrize(
    "fields",
    [
        None,
        {},
        {"id": "other"},
        {"projectIds": []},
        {"name": " "},
        {"name": True},
        {"description": None},
        {"color": []},
        {"color": "purple"},
        {"date": None},
        {"date": "2026-02-29"},
        {"date": "20261005"},
        {"date": "2026-1-5"},
        {"startDate": "2026-13-01"},
        {"date": 1},
    ],
)
def test_update_milestone_rejects_invalid_fields(fields):
    with patch("codecks_cli.admin.api.dispatch") as dispatch:
        with pytest.raises(CliError) as error:
            update_milestone("00000000-0000-0000-0000-000000000001", fields)
        assert error.value.error_code == "INVALID_INPUT"
        dispatch.assert_not_called()


@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"milestone": {}},
        {"milestone": {"00000000-0000-0000-0000-000000000001": {"name": "Old"}}},
        CliError("read failed", error_code="NETWORK_ERROR", retryable=True),
    ],
)
def test_update_milestone_unverified_write_is_not_retryable(reply):
    mid = "00000000-0000-0000-0000-000000000001"
    config._cache["milestone_records"] = {}
    with (
        patch("codecks_cli.admin.api.dispatch", return_value={}),
        patch("codecks_cli.admin.api.query") as query,
    ):
        if isinstance(reply, Exception):
            query.side_effect = reply
        else:
            query.return_value = reply
        with pytest.raises(CliError) as error:
            update_milestone(mid, {"name": "New", "startDate": None})
        assert (
            error.value.error_code == "MILESTONE_UPDATE_UNVERIFIED"
            and error.value.retryable is False
        )
    assert "milestone_records" not in config._cache


def _caching_list_decks(decks):
    """Mimic cards.list_decks(): populate config._cache["decks"] and return it."""

    result = {"deck": dict(decks)}

    def _call():
        config._cache["decks"] = result
        return result

    return _call


class TestCreateDeckExistingCheck:
    """The duplicate check must read the deck's project id in either spelling."""

    @patch("codecks_cli.admin.api.dispatch")
    @patch("codecks_cli.cards.list_decks")
    @patch("codecks_cli.admin._resolve_project_id", return_value=_PID)
    def test_detects_existing_deck_snake_case_project_id(self, _pid, mock_decks, mock_dispatch):
        mock_decks.return_value = {
            "deck": {"d-1": {"id": "d-1", "title": "Code", "project_id": _PID}}
        }
        result = create_deck("code", project="Tea Shop")
        assert result["ok"] is True
        assert result["already_existed"] is True
        assert result["deck_id"] == "d-1"
        mock_dispatch.assert_not_called()

    @patch("codecks_cli.admin.api.dispatch")
    @patch("codecks_cli.cards.list_decks")
    @patch("codecks_cli.admin._resolve_project_id", return_value=_PID)
    def test_detects_existing_deck_camel_case_project_id(self, _pid, mock_decks, mock_dispatch):
        mock_decks.return_value = {
            "deck": {"d-1": {"id": "d-1", "title": "Code", "projectId": _PID}}
        }
        result = create_deck("Code", project="Tea Shop")
        assert result["already_existed"] is True
        mock_dispatch.assert_not_called()

    @patch("codecks_cli.admin.api.dispatch")
    @patch("codecks_cli.cards.list_decks")
    @patch("codecks_cli.admin._resolve_project_id", return_value=_PID)
    def test_same_name_other_project_still_creates(self, _pid, mock_decks, mock_dispatch):
        mock_decks.return_value = {
            "deck": {"d-1": {"id": "d-1", "title": "Code", "project_id": "other-project"}}
        }
        mock_dispatch.return_value = {"payload": {"id": "d-new"}}
        result = create_deck("Code", project="Tea Shop")
        assert result["already_existed"] is False
        mock_dispatch.assert_called_once()


class TestCreateDeckCacheSeeding:
    """A freshly created deck must land in the deck cache, not be popped away."""

    @patch("codecks_cli.admin.api.dispatch")
    @patch("codecks_cli.cards.list_decks")
    @patch("codecks_cli.admin._resolve_project_id", return_value=_PID)
    def test_new_deck_seeded_into_cache(self, _pid, mock_decks, mock_dispatch):
        mock_decks.side_effect = _caching_list_decks(
            {"d-1": {"id": "d-1", "title": "Code", "projectId": _PID}}
        )
        mock_dispatch.return_value = {"payload": {"id": "d-new"}}

        result = create_deck("Audio", project="Tea Shop")

        assert result["ok"] is True
        assert result["deck_id"] == "d-new"
        seeded = config._cache["decks"]["deck"]["d-new"]
        assert seeded == {"id": "d-new", "title": "Audio", "projectId": _PID}
        # Pre-existing decks survive the seed.
        assert "d-1" in config._cache["decks"]["deck"]

    @patch("codecks_cli.admin.api.dispatch")
    @patch("codecks_cli.cards.list_decks")
    @patch("codecks_cli.admin._resolve_project_id", return_value=_PID)
    def test_resolve_deck_id_finds_new_deck_immediately(self, _pid, mock_decks, mock_dispatch):
        from codecks_cli.cards import resolve_deck_id

        mock_decks.side_effect = _caching_list_decks({})
        mock_dispatch.return_value = {"payload": {"id": "d-new"}}

        create_deck("Audio", project="Tea Shop")

        # resolve_deck_id goes through the real list_decks(), which returns the
        # seeded config._cache["decks"] without another API round-trip.
        assert resolve_deck_id("Audio") == "d-new"

    @patch("codecks_cli.admin.api.dispatch")
    @patch("codecks_cli.cards.list_decks")
    @patch("codecks_cli.admin._resolve_project_id", return_value=_PID)
    def test_seeds_even_when_cache_absent(self, _pid, mock_decks, mock_dispatch):
        mock_decks.return_value = {"deck": {}}  # does not populate the cache
        config._cache.pop("decks", None)
        mock_dispatch.return_value = {"payload": {"id": "d-new"}}

        create_deck("Audio", project="Tea Shop")

        assert config._cache["decks"]["deck"]["d-new"]["title"] == "Audio"

    @patch("codecks_cli.admin.api.dispatch")
    @patch("codecks_cli.cards.list_decks")
    @patch("codecks_cli.admin._resolve_project_id", return_value=_PID)
    def test_missing_deck_id_invalidates_cache(self, _pid, mock_decks, mock_dispatch):
        mock_decks.side_effect = _caching_list_decks(
            {"d-1": {"id": "d-1", "title": "Code", "projectId": _PID}}
        )
        mock_dispatch.return_value = {"payload": {}}

        result = create_deck("Audio", project="Tea Shop")

        assert result["ok"] is True
        assert "decks" not in config._cache


class TestMilestoneCreator:
    @patch("codecks_cli.admin.api.dispatch", return_value={"payload": {"id": "m1"}})
    @patch("codecks_cli.admin._resolve_project_id", return_value="p1")
    @patch("codecks_cli.admin._get_account_id", return_value="acc")
    def test_milestone_creator_is_token_user(self, _acc, _proj, mock_dispatch, monkeypatch):
        # admin used to take the first user of account.roles, which may be someone else.
        from codecks_cli.admin import create_milestone

        monkeypatch.setattr("codecks_cli.cards.config.USER_ID", "me-id")
        with patch("codecks_cli.config.save_env_value"):
            create_milestone("M1")
        assert mock_dispatch.call_args.args[1]["userId"] == "me-id"
