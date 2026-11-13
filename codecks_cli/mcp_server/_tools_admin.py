"""Admin tools: project, deck, milestone, tag creation and metadata editing (7 tools).

Uses direct Codecks dispatch API endpoints. All calls are offloaded to a worker
thread via asyncio.to_thread() since the MCP server runs in an asyncio event loop.
"""

import asyncio

from codecks_cli import CliError, config
from codecks_cli.mcp_server._core import (
    _call,
    _contract_error,
    _ensure_cache_scope,
    _finalize_tool_result,
    _invalidate_cache,
    _persist_cache_to_disk,
)
from codecks_cli.mcp_server._security import _validate_input


def _run_admin(func, *args, **kwargs):
    """Call a sync admin function (dispatch API) and return its result."""
    _ensure_cache_scope()
    config._cache.clear()
    try:
        result = func(*args, **kwargs)
    except CliError as exc:
        return _finalize_tool_result(_contract_error(str(exc), "error", exc=exc))
    if result.get("ok"):
        _invalidate_cache()
        _persist_cache_to_disk()
    return _finalize_tool_result(result)


async def create_project(name: str) -> dict:
    """Create a new Codecks project.

    Uses direct Codecks dispatch API.

    Args:
        name: Project name (e.g., "Hafu Games Studio").

    Returns:
        Dict with ok, project_name, source.
    """
    try:
        name = _validate_input(name, "name")
    except CliError as e:
        return _finalize_tool_result(_contract_error(str(e), "error"))

    from codecks_cli import admin

    return await asyncio.to_thread(_run_admin, admin.create_project, name)


async def create_deck(name: str, project: str | None = None) -> dict:
    """Create a new deck in a project.

    Uses direct Codecks dispatch API.

    Args:
        name: Deck name (e.g., "Publishing").
        project: Project name. Defaults to the primary project.

    Returns:
        Dict with ok, deck_name, project_name, source.
    """
    try:
        name = _validate_input(name, "name")
        if project is not None:
            project = _validate_input(project, "project")
    except CliError as e:
        return _finalize_tool_result(_contract_error(str(e), "error"))

    from codecks_cli import admin

    return await asyncio.to_thread(_run_admin, admin.create_deck, name, project=project)


async def create_milestone(
    name: str,
    target_date: str | None = None,
    project: str | None = None,
    color: str = "blue",
) -> dict:
    """Create a release milestone.

    Uses direct Codecks dispatch API and API-backed name resolution.

    Args:
        name: Milestone name (e.g., "Alpha").
        target_date: Optional target date (YYYY-MM or YYYY-MM-DD).
        project: Project name. Defaults to the primary project.
        color: Milestone color: gray, brown, yellow, red, pink, blue, green.
               Default: blue.

    Returns:
        Dict with ok, milestone_name, milestone_id, color, source.
    """
    from codecks_cli.admin import VALID_MILESTONE_COLORS as _API_VALID_COLORS

    if color and color.lower().strip() not in _API_VALID_COLORS:
        return _finalize_tool_result(
            _contract_error(
                f"Invalid milestone color '{color}'. "
                f"Valid: {', '.join(sorted(_API_VALID_COLORS))}. Default: blue.",
                "error",
                error_code="INVALID_INPUT",
            )
        )

    try:
        name = _validate_input(name, "name")
        if target_date is not None:
            target_date = _validate_input(target_date, "target_date")
        if project is not None:
            project = _validate_input(project, "project")
    except CliError as e:
        return _finalize_tool_result(_contract_error(str(e), "error"))

    from codecks_cli import admin

    return await asyncio.to_thread(
        _run_admin,
        admin.create_milestone,
        name,
        target_date=target_date,
        project=project,
        color=color,
    )


async def create_tag(name: str, project: str | None = None) -> dict:
    """Create a new project-level tag.

    Uses direct Codecks dispatch API. The `projects/addTag` endpoint has
    no color field, so tag colors cannot be set from here.

    Args:
        name: Tag name (e.g., "legal").
        project: Project name. Defaults to the primary project.

    Returns:
        Dict with ok, tag_name, source.
    """
    try:
        name = _validate_input(name, "name")
        if project is not None:
            project = _validate_input(project, "project")
    except CliError as e:
        return _finalize_tool_result(_contract_error(str(e), "error"))

    from codecks_cli import admin

    return await asyncio.to_thread(_run_admin, admin.create_tag, name, project=project)


async def archive_deck(deck: str) -> dict:
    """Delete a deck. NOT reversible.

    The Codecks dispatch API has no deck-archive action, so this dispatches
    `decks/delete`. The deck is gone for good; its cards are preserved.

    Args:
        deck: Deck name to delete.

    Returns:
        Dict with ok, deck_name, source.
    """
    try:
        deck = _validate_input(deck, "deck")
    except CliError as e:
        return _finalize_tool_result(_contract_error(str(e), "error"))

    from codecks_cli import admin

    return await asyncio.to_thread(_run_admin, admin.archive_deck, deck)


async def update_deck(deck_id: str, fields: dict, allow_card_changes: bool = False) -> dict:
    """Edit title, description, deckType, defaultCard or milestoneId by full deck UUID.

    milestoneId: UUID or null. Setting moves all active cards into the milestone;
    clearing removes it only from cards matching the deck's old milestone.
    Affected cards require allow_card_changes=true after inspecting the reported count.
    The target must support the deck's project. Deck and card counts are verified.
    Reassigning the current milestone cannot move drifted cards; update those cards directly.
    deckType: task, hero, doc, mixed. Switching to hero resets started cards.
    Hero/doc changes fail if Journey steps target this deck. defaultCard replaces
    the entire template; {} clears it. This template write is undocumented by
    Codecks, and API card creation does not apply template defaults. Read current
    values with list_decks(include_metadata=True). Template writes require a
    matching read-back; DECK_UPDATE_UNVERIFIED means inspect state before retrying.
    """
    result = await asyncio.to_thread(
        _call, "update_deck", deck_id=deck_id, fields=fields, allow_card_changes=allow_card_changes
    )
    if result.get("ok") is False and result.get("error_code") not in (
        "INVALID_INPUT",
        "DECK_MILESTONE_REQUIRES_ACK",
    ):
        # A failed response can follow a completed write; do not reuse stale data.
        _invalidate_cache()
        _persist_cache_to_disk()
    return _finalize_tool_result(result)


async def update_milestone(
    milestone_id: str, fields: dict, allow_project_removal: bool = False
) -> dict:
    """Edit milestone name, description, color, date or startDate by full UUID.

    Omitted fields remain unchanged. description="" clears text; startDate=null
    clears the start date. Dates must be YYYY-MM-DD. Colors: gray, brown, yellow,
    red, pink, blue, green. Reads back all submitted fields before success.
    MILESTONE_UPDATE_UNVERIFIED means inspect fresh metadata before retrying.
    isGlobal=true enables all projects; a nonempty projectIds replaces the project list
    and makes the milestone non-global. Do not combine both. Removing projects
    also removes their card/deck associations and requires allow_project_removal=true.
    A global-to-local transition always requires this acknowledgement.
    """
    result = await asyncio.to_thread(
        _call,
        "update_milestone",
        milestone_id=milestone_id,
        fields=fields,
        allow_project_removal=allow_project_removal,
    )
    if result.get("ok") is False and result.get("error_code") not in (
        "INVALID_INPUT",
        "PROJECT_REMOVAL_REQUIRES_ACK",
    ):
        _invalidate_cache()
        _persist_cache_to_disk()
    return _finalize_tool_result(result)


def register(mcp):
    """Register all admin tools with the MCPServer instance."""
    mcp.tool()(create_project)
    mcp.tool()(create_deck)
    mcp.tool()(create_milestone)
    mcp.tool()(create_tag)
    mcp.tool()(archive_deck)
    mcp.tool()(update_deck)
    mcp.tool()(update_milestone)
