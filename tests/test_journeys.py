"""Native Journey payloads, validation and MCP error/cache contracts."""

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from codecks_cli import journeys
from codecks_cli.client import CodecksClient
from codecks_cli.exceptions import CliError

DECK = "00000000-0000-0000-0000-000000000001"
STEP = "00000000-0000-0000-0000-000000000002"
USER = "00000000-0000-0000-0000-000000000003"
CARD = "00000000-0000-0000-0000-000000000004"
ACCOUNT = "00000000-0000-0000-0000-000000000005"


@pytest.fixture(autouse=True)
def clear_pending_starts():
    journeys._pending_starts.clear()
    yield
    journeys._pending_starts.clear()


def test_read_uses_compound_deck_id_and_preserves_step_order():
    data = {
        "deck": {
            DECK: {
                "title": "Characters",
                "workflowItemOrderLabels": ["Produce"],
                "workflowItems": [STEP, CARD],
            }
        },
        "workflowItem": {
            STEP: {"content": "Later", "sortValue": 2},
            CARD: {"content": "Earlier", "sortValue": 1},
        },
    }
    with patch("codecks_cli.journeys.query", return_value=data) as request:
        result = CodecksClient(validate_token=False).get_journey(DECK)
    assert list(request.call_args.args[0]) == [f'deck(["{DECK}"])']
    assert [step["id"] for step in result["steps"]] == [CARD, STEP]
    assert result["zones"] == ["Produce"]


def test_create_update_order_use_native_endpoints_and_preserve_null_clears():
    client = CodecksClient(validate_token=False)
    with (
        patch(
            "codecks_cli.journeys.session_request", return_value={"actionId": "action"}
        ) as request,
        patch(
            "codecks_cli.journeys.get_journey",
            return_value={"steps": [{"id": STEP, "label": "Produce"}]},
        ),
    ):
        client.create_journey_step(
            DECK, "Concept", user_id=USER, fields={"targetDeckId": CARD, "effort": 0}
        )
        assert request.call_args.args == (
            "/dispatch/workflows/createItem",
            {
                "deckId": DECK,
                "userId": USER,
                "content": "Concept",
                "targetDeckId": CARD,
                "effort": 0,
            },
        )
        client.update_journey_step(STEP, {"assigneeId": None, "visibility": "deleted"})
        assert request.call_args.args == (
            "/dispatch/workflows/updateItem",
            {"itemId": STEP, "assigneeId": None, "visibility": "deleted"},
        )
        client.order_journey_steps(DECK, [STEP], None, "Produce")
        assert request.call_args.args == (
            "/dispatch/workflows/addItemsAfter",
            {"deckId": DECK, "itemIds": [STEP], "targetId": None, "label": "Produce"},
        )


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"unknown": 1},
        {"effort": True},
        {"effort": -1},
        {"priority": "high"},
        {"content": " "},
        {"targetDeckId": "bad"},
        {"masterTags": None},
        {"visibility": "archived"},
        {"inDeps": [STEP]},
    ],
)
def test_invalid_update_never_dispatches(fields):
    with patch("codecks_cli.journeys.session_request") as request:
        with pytest.raises(CliError) as error:
            journeys.update_journey_step(STEP, fields)
        assert error.value.error_code == "INVALID_INPUT"
        request.assert_not_called()


@pytest.mark.parametrize(
    "items,target", [([], None), ([STEP, STEP], None), ([STEP], STEP), (["bad"], None)]
)
def test_invalid_order_never_dispatches(items, target):
    with patch("codecks_cli.journeys.session_request") as request:
        with pytest.raises(CliError):
            journeys.order_journey_steps(DECK, items, target, None)
        request.assert_not_called()


def test_start_resolves_card_reference_before_applying_and_does_not_retry_write():
    with (
        patch(
            "codecks_cli.journeys.resolve_card_ids", return_value={"cards": [{"id": CARD}]}
        ) as resolve,
        patch("codecks_cli.journeys._get_user_id", return_value=USER),
        patch("codecks_cli.journeys.get_account", return_value={"account": {ACCOUNT: {}}}),
        patch(
            "codecks_cli.journeys._read_journey_parent", side_effect=[(DECK, set()), (DECK, {STEP})]
        ),
        patch("codecks_cli.journeys.get_journey", return_value={"steps": [{"id": STEP}]}),
        patch("codecks_cli.journeys.session_request", return_value={}) as request,
    ):
        assert journeys.start_journey("$1uc")["ok"]
    resolve.assert_called_once_with(["$1uc"])
    request.assert_called_once_with(
        "/dispatch/workflows/apply",
        {"cardId": CARD, "itemIds": [STEP], "userId": USER, "accountId": ACCOUNT},
    )


def test_deleted_and_archived_steps_are_excluded():
    data = {
        "deck": {DECK: {"workflowItems": [STEP, CARD, USER]}},
        "workflowItem": {
            STEP: {"visibility": "default", "sortValue": "U"},
            CARD: {"visibility": "deleted", "sortValue": "j"},
            USER: {"visibility": "archived", "sortValue": "r"},
        },
    }
    with patch("codecks_cli.journeys.query", return_value=data):
        assert [step["id"] for step in journeys.get_journey(DECK)["steps"]] == [STEP]


def test_move_to_beginning_anchors_before_first_unmoved_step_in_zone():
    steps = [
        {"id": USER, "label": "Other"},
        {"id": CARD, "label": "Produce"},
        {"id": STEP, "label": "Produce"},
    ]
    with (
        patch("codecks_cli.journeys.get_journey", return_value={"steps": steps}),
        patch("codecks_cli.journeys.session_request", return_value={}) as request,
    ):
        journeys.order_journey_steps(DECK, [STEP], None, "Produce")
    request.assert_called_once_with(
        "/dispatch/workflows/addItemsBefore",
        {"deckId": DECK, "itemIds": [STEP], "targetId": CARD, "label": "Produce"},
    )


def test_order_rejects_steps_from_another_deck_before_write():
    with (
        patch("codecks_cli.journeys.get_journey", return_value={"steps": []}),
        patch("codecks_cli.journeys.session_request") as request,
    ):
        with pytest.raises(CliError, match="this deck"):
            journeys.order_journey_steps(DECK, [STEP], None, None)
    request.assert_not_called()


def test_concurrent_starts_re_read_inside_parent_lock():
    entered = threading.Event()
    release = threading.Event()
    second_resolved = threading.Event()
    applied = set()

    def resolve(refs):
        if refs == ["#4"]:
            second_resolved.set()
        return {"cards": [{"id": CARD}]}

    def dispatch(*args):
        entered.set()
        assert release.wait(5)
        applied.add(STEP)
        return {}

    with (
        patch("codecks_cli.journeys.resolve_card_ids", side_effect=resolve),
        patch(
            "codecks_cli.journeys._read_journey_parent",
            side_effect=lambda _: (DECK, applied.copy()),
        ),
        patch("codecks_cli.journeys.get_journey", return_value={"steps": [{"id": STEP}]}),
        patch("codecks_cli.journeys._get_user_id", return_value=USER),
        patch("codecks_cli.journeys.get_account", return_value={"account": {ACCOUNT: {}}}),
        patch("codecks_cli.journeys.session_request", side_effect=dispatch) as request,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        first = pool.submit(journeys.start_journey, CARD)
        try:
            assert entered.wait(5)
            second = pool.submit(journeys.start_journey, "#4")
            assert second_resolved.wait(5)
            assert not second.done()
        finally:
            release.set()
        assert first.result(timeout=5)["ok"]
        assert second.result(timeout=5)["created"] == 0
    request.assert_called_once()


@pytest.mark.parametrize("failure", ["timeout", "verification_read", "missing_children"])
def test_uncertain_start_blocks_writes_until_submitted_steps_are_visible(failure):
    applied = set()
    read_count = 0

    def read(_):
        nonlocal read_count
        read_count += 1
        if failure == "verification_read" and read_count == 2:
            raise CliError("read failed", error_code="NETWORK_ERROR")
        return DECK, applied.copy()

    with (
        patch("codecks_cli.journeys.resolve_card_ids", return_value={"cards": [{"id": CARD}]}),
        patch("codecks_cli.journeys._read_journey_parent", side_effect=read),
        patch("codecks_cli.journeys.get_journey", return_value={"steps": [{"id": STEP}]}),
        patch("codecks_cli.journeys._get_user_id", return_value=USER),
        patch("codecks_cli.journeys.get_account", return_value={"account": {ACCOUNT: {}}}),
        patch("codecks_cli.journeys.session_request", return_value={}) as request,
    ):
        if failure == "timeout":
            request.side_effect = CliError("timed out", error_code="NETWORK_ERROR")
        for _ in range(2):
            with pytest.raises(CliError) as error:
                journeys.start_journey(CARD)
            assert error.value.error_code == "JOURNEY_OUTCOME_UNKNOWN"
            assert error.value.retryable is False
        request.assert_called_once()
        applied.add(STEP)
        assert journeys.start_journey(CARD)["created"] == 0
        assert CARD not in journeys._pending_starts
        request.assert_called_once()


def test_start_skips_previously_applied_steps_and_is_noop_when_complete():
    data = {
        "card": {
            CARD: {"deckId": DECK, "childCards": [USER]},
            USER: {"sourceWorkflowItemId": STEP},
        }
    }
    with (
        patch("codecks_cli.journeys.resolve_card_ids", return_value={"cards": [{"id": CARD}]}),
        patch("codecks_cli.journeys.query", return_value=data),
        patch("codecks_cli.journeys.get_journey", return_value={"steps": [{"id": STEP}]}),
        patch("codecks_cli.journeys.session_request") as request,
    ):
        assert journeys.start_journey(CARD)["created"] == 0
    request.assert_not_called()


def test_mcp_denial_preserves_scope_error_without_falling_back():
    from codecks_cli.mcp_server import _core, update_journey_step

    with (
        patch.object(_core, "_get_client", return_value=CodecksClient(validate_token=False)),
        patch(
            "codecks_cli.journeys.session_request",
            side_effect=CliError("requires workflowTemplate:write", error_code="PERMISSION_DENIED"),
        ) as request,
    ):
        result = update_journey_step(STEP, {"content": "Concept"})
    assert result["error_code"] == "PERMISSION_DENIED"
    assert result["retryable"] is False
    request.assert_called_once()


def test_successful_journey_write_invalidates_card_snapshot():
    from codecks_cli.mcp_server import _core, update_journey_step

    _core._snapshot_cache = {"cards_result": {"cards": []}}
    with (
        patch.object(_core, "_get_client", return_value=CodecksClient(validate_token=False)),
        patch("codecks_cli.journeys.session_request", return_value={}),
    ):
        assert update_journey_step(STEP, {"content": "Concept"})["ok"]
    assert "cards_result" not in _core._snapshot_cache


def test_uncertain_journey_write_invalidates_snapshot_and_is_not_retryable():
    from codecks_cli.mcp_server import _core, start_journey

    _core._snapshot_cache = {"cards_result": {"cards": []}}
    with (
        patch.object(_core, "_get_client", return_value=CodecksClient(validate_token=False)),
        patch("codecks_cli.journeys.start_journey", side_effect=journeys._uncertain_start(CARD)),
    ):
        result = start_journey(CARD)
    assert result["error_code"] == "JOURNEY_OUTCOME_UNKNOWN"
    assert result["retryable"] is False
    assert "cards_result" not in _core._snapshot_cache


def test_mcp_read_tags_user_content_and_labels():
    from codecks_cli.mcp_server import _core, get_journey

    data = {
        "deck": {
            DECK: {
                "title": "Characters",
                "workflowItems": [STEP],
                "workflowItemOrderLabels": ["Produce"],
            }
        },
        "workflowItem": {STEP: {"content": "Concept", "label": "Produce"}},
    }
    with (
        patch.object(_core, "_get_client", return_value=CodecksClient(validate_token=False)),
        patch("codecks_cli.journeys.query", return_value=data),
    ):
        result = get_journey(DECK)
    assert result["steps"][0]["content"] == "[USER_DATA]Concept[/USER_DATA]"
    assert result["zones"] == ["[USER_DATA]Produce[/USER_DATA]"]


def test_mcp_write_tags_nested_dispatch_content():
    from codecks_cli.mcp_server import _core, update_journey_step

    with (
        patch.object(_core, "_get_client", return_value=CodecksClient(validate_token=False)),
        patch(
            "codecks_cli.journeys.session_request",
            return_value={"workflowItem": {STEP: {"content": "Concept", "label": "Produce"}}},
        ),
    ):
        result = update_journey_step(STEP, {"content": "Concept"})
    assert result["response"]["workflowItem"][STEP]["content"] == "[USER_DATA]Concept[/USER_DATA]"
    assert result["response"]["workflowItem"][STEP]["label"] == "[USER_DATA]Produce[/USER_DATA]"
