"""Admin operations — direct dispatch API calls.

Uses Codecks dispatch endpoints discovered via network inspection.
No Playwright dependency required.

Discovered endpoints and their field names:
  - projects/addTag:    {projectId, tag}
  - projects/create:    {name, accountId, defaultUserAccess} (enum: "everyone")
      Returns: {payload: {id: "<uuid>"}}
  - decks/create:       {projectId, title}
      Returns: {payload: {id: "<uuid>"}}
  - decks/delete:       {id}       (NOTE: uses 'id', not 'deckId')
  - milestones/create:  {name, userId, accountId, color, date, isGlobal, projectIds}
      color: one of "blue", "green", "red", "yellow", "purple", "orange", "pink", "teal"
      date: ISO date string (YYYY-MM-DD)
      projectIds: array of project UUIDs
      Returns: {payload: {id: "<uuid>"}}
  - milestones/delete:  {id}
  - cards/update:       {id, ...}  (NOTE: uses 'id', not 'cardId')
"""

import json
from datetime import date
from typing import Any
from uuid import UUID

from codecks_cli import api, config
from codecks_cli._utils import _get_field
from codecks_cli.cards import _get_user_id
from codecks_cli.exceptions import CliError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_admin_uuid(value: Any, name: str) -> str:
    try:
        if isinstance(value, str) and str(UUID(value)) == value.lower():
            return value.lower()
    except ValueError:
        pass
    raise CliError(f"{name} must be a full UUID", error_code="INVALID_INPUT", retryable=False)


def _get_account_id() -> str:
    """Get the account UUID via the Codecks query API."""
    result = api.query({"_root": [{"account": ["id"]}]})
    for _key, acc in result.get("account", {}).items():
        return str(acc["id"])
    raise CliError("[ERROR] Could not resolve account ID.")


def _get_primary_project_id() -> str:
    """Resolve the primary project ID by querying decks.

    Returns the projectId that owns the most decks (heuristic for primary).
    """
    from codecks_cli.cards import list_projects

    projects = list_projects()
    if not projects:
        raise CliError("[ERROR] No projects found. Cannot resolve project ID.")

    best_pid = max(projects, key=lambda pid: projects[pid].get("deck_count", 0))
    return str(best_pid)


def _resolve_project_id(project_name: str | None = None) -> str:
    """Resolve a project name to its UUID.

    Args:
        project_name: Project name (case-insensitive). If None, returns primary project.

    Returns:
        Project UUID.
    """
    if project_name is None:
        return _get_primary_project_id()

    from codecks_cli.cards import list_projects

    projects = list_projects()
    for pid, info in projects.items():
        if info.get("name", "").lower() == project_name.lower():
            return str(pid)

    raise CliError(
        f"[ERROR] Project '{project_name}' not found. "
        f"Available: {[info.get('name') for info in projects.values()]}"
    )


def _resolve_deck_id(deck_name: str, project: str | None = None) -> str:
    """Resolve a deck name to its UUID.

    Args:
        deck_name: Deck name (case-insensitive).
        project: Optional project name to scope resolution.
    """
    from codecks_cli.cards import resolve_deck_id

    return str(resolve_deck_id(deck_name, project=project))


# ---------------------------------------------------------------------------
# Admin operations
# ---------------------------------------------------------------------------


def create_tag(name: str, color: str | None = None, project: str | None = None) -> dict[str, Any]:
    """Create a new project-level tag via dispatch API.

    Args:
        name: Tag name.
        color: Accepted for signature compatibility and ignored — the
            `projects/addTag` endpoint has no color field.
        project: Project name (defaults to primary project).

    Returns:
        Dict with ok, tag_name, and source.
    """
    project_id = _resolve_project_id(project)
    payload: dict[str, Any] = {"projectId": project_id, "tag": name}

    try:
        api.dispatch("projects/addTag", payload)
        return {"ok": True, "tag_name": name, "source": "api"}
    except CliError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": e.error_code or "CLI_ERROR",
            "retryable": e.retryable,
        }


def create_deck(name: str, project: str | None = None) -> dict[str, Any]:
    """Create a new deck in a project via dispatch API.

    Idempotent: if a deck with the same name already exists in the project,
    returns success with ``already_existed: True`` instead of creating a duplicate.

    Args:
        name: Deck name.
        project: Project name (defaults to primary project).

    Returns:
        Dict with ok, deck_name, project_name, and source.
    """
    project_id = _resolve_project_id(project)
    project_name = project or "primary"

    # Check for existing deck with the same name in the same project.
    config._cache.pop("decks", None)  # Force fresh query
    from codecks_cli.cards import list_decks

    existing = list_decks()
    for _key, deck in existing.get("deck", {}).items():
        deck_pid = _get_field(deck, "project_id", "projectId")
        if deck.get("title", "").lower() == name.lower() and deck_pid == project_id:
            return {
                "ok": True,
                "already_existed": True,
                "deck_name": deck.get("title", name),
                "deck_id": deck.get("id", _key),
                "project_name": project_name,
                "source": "cache",
            }

    payload = {"projectId": project_id, "title": name}

    try:
        result = api.dispatch("decks/create", payload)
        deck_id = result.get("payload", {}).get("id", "")
        # Seed the new deck into the deck cache so an immediate
        # resolve_deck_id() finds it without waiting for API consistency.
        # Build the seeded entry from the dict just returned by list_decks()
        # (which is what config._cache["decks"] holds) — popping first would
        # drop the seed on the floor.
        cached = config._cache.get("decks")
        if not (isinstance(cached, dict) and isinstance(cached.get("deck"), dict)):
            cached = {"deck": {}}
        if deck_id:
            deck_map: dict[str, Any] = cached["deck"]
            deck_map[deck_id] = {
                "id": deck_id,
                "title": name,
                "projectId": project_id,
            }
            config._cache["decks"] = cached
        else:
            config._cache.pop("decks", None)  # Invalidate so next query re-fetches
        return {
            "ok": True,
            "already_existed": False,
            "deck_name": name,
            "deck_id": deck_id,
            "project_name": project_name,
            "source": "api",
        }
    except CliError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": e.error_code or "CLI_ERROR",
            "retryable": e.retryable,
        }


VALID_MILESTONE_COLORS = {"gray", "brown", "yellow", "red", "pink", "blue", "green"}


def create_milestone(
    name: str,
    target_date: str | None = None,
    project: str | None = None,
    color: str = "blue",
) -> dict[str, Any]:
    """Create a release milestone via dispatch API.

    Invalidates API-backed milestone records after creation.

    Args:
        name: Milestone name.
        target_date: Target date (YYYY-MM-DD). Defaults to 30 days from now.
        project: Project name (defaults to primary project).
        color: Milestone color: gray, brown, yellow, red, pink, blue, green.

    Returns:
        Dict with ok, milestone_name, milestone_id, and source.
    """
    import datetime

    # Validate color
    color = color.lower().strip()
    if color not in VALID_MILESTONE_COLORS:
        return {
            "ok": False,
            "error": (
                f"Invalid milestone color '{color}'. "
                f"Valid colors: {', '.join(sorted(VALID_MILESTONE_COLORS))}"
            ),
        }

    account_id = _get_account_id()
    user_id = _get_user_id()
    project_id = _resolve_project_id(project)

    if not target_date:
        target_date = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()

    payload = {
        "name": name,
        "userId": user_id,
        "accountId": account_id,
        "color": color,
        "date": target_date,
        "isGlobal": False,
        "projectIds": [project_id],
    }

    try:
        result = api.dispatch("milestones/create", payload)
        milestone_id = result.get("payload", {}).get("id", "")

        config._cache.pop("milestone_records", None)

        return {
            "ok": True,
            "milestone_name": name,
            "milestone_id": milestone_id,
            "color": color,
            "source": "api",
        }
    except CliError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": e.error_code or "CLI_ERROR",
            "retryable": e.retryable,
        }


def create_project(name: str) -> dict[str, Any]:
    """Create a new Codecks project via dispatch API.

    Auto-registers the new project in ``.env`` and in-memory config so it's
    immediately resolvable by name without a server restart.

    Args:
        name: Project name.

    Returns:
        Dict with ok, project_name, project_id, and source.
    """
    account_id = _get_account_id()
    payload = {
        "name": name,
        "accountId": account_id,
        "defaultUserAccess": "everyone",
    }

    try:
        result = api.dispatch("projects/create", payload)
        project_id = result.get("payload", {}).get("id", "")

        # Refresh the API-backed project list after creation.
        if project_id:
            config._cache.pop("project_records", None)
            config._cache.pop("project_names", None)
            config._cache.pop("decks", None)

        return {
            "ok": True,
            "project_name": name,
            "project_id": project_id,
            "source": "api",
        }
    except CliError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": e.error_code or "CLI_ERROR",
            "retryable": e.retryable,
        }


def update_deck(deck_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Update deck metadata. defaultCard is an experimental whole-template replacement."""

    def invalid(message: str) -> None:
        raise CliError(message, error_code="INVALID_INPUT", retryable=False)

    deck_id = _validate_admin_uuid(deck_id, "deck_id")
    allowed = {"title", "description", "deckType", "defaultCard"}
    if not isinstance(fields, dict) or not fields or fields.keys() - allowed:
        invalid(
            "fields must be a nonempty object containing title, description, deckType or defaultCard"
        )
    for key, maximum in (("title", 500), ("description", 10000)):
        if key in fields:
            value = fields[key]
            if (
                not isinstance(value, str)
                or len(value) > maximum
                or (key == "title" and not value.strip())
            ):
                invalid(
                    f"{key} must be a string of at most {maximum} characters; title cannot be empty"
                )
    if "deckType" in fields and fields["deckType"] not in ("task", "hero", "doc", "mixed"):
        invalid("deckType must be task, hero, doc or mixed")
    if "defaultCard" in fields:
        template = fields["defaultCard"]
        if not isinstance(template, dict) or template.keys() - {
            "content",
            "assigneeId",
            "priority",
            "effort",
            "masterTags",
        }:
            invalid("defaultCard contains unsupported fields")
        if "content" in template and (
            not isinstance(template["content"], str) or len(template["content"]) > 50000
        ):
            invalid("defaultCard.content must be a string of at most 50000 characters")
        if template.get("priority") not in (None, "a", "b", "c"):
            invalid("defaultCard.priority must be a, b, c or null")
        effort = template.get("effort")
        if effort is not None and (type(effort) is not int or not 0 <= effort <= 2147483647):
            invalid("defaultCard.effort must be a nonnegative int32 or null")
        if template.get("assigneeId") is not None:
            _validate_admin_uuid(template["assigneeId"], "defaultCard.assigneeId")
        if "masterTags" in template:
            tags = template["masterTags"]
            if (
                not isinstance(tags, list)
                or len(tags) > 100
                or any(not isinstance(t, str) or not t.strip() or len(t) > 500 for t in tags)
            ):
                invalid(
                    "defaultCard.masterTags must contain at most 100 nonempty strings of at most 500 characters"
                )
    try:
        api.dispatch("decks/update", {"id": deck_id.lower(), **fields})
        if "defaultCard" in fields:
            try:
                data = api.query({f"deck({json.dumps([deck_id.lower()])})": ["defaultCard"]})
                deck = data.get("deck", {}).get(deck_id.lower(), {})
                actual = _get_field(deck, "default_card", "defaultCard")
                if not isinstance(actual, dict) or actual != fields["defaultCard"]:
                    raise CliError("Template read-back does not match the requested replacement")
            except (CliError, AttributeError, TypeError) as error:
                raise CliError(
                    "Deck update was submitted, but the template could not be verified. "
                    "Read the deck before retrying.",
                    error_code="DECK_UPDATE_UNVERIFIED",
                    retryable=False,
                ) from error
    finally:
        config._cache.pop("decks", None)
    return {"ok": True, "deck_id": deck_id.lower(), "updated_fields": list(fields), "source": "api"}


def _prepare_milestone_scope(milestone_id, fields, allow_project_removal):
    from codecks_cli.cards import _milestone_scope, list_projects

    if "isGlobal" in fields and type(fields["isGlobal"]) is not bool:
        raise CliError("isGlobal must be a boolean", error_code="INVALID_INPUT")
    if fields.get("isGlobal") is True and "projectIds" in fields:
        raise CliError("Do not combine isGlobal=true with projectIds", error_code="INVALID_INPUT")
    if "projectIds" in fields:
        project_ids = fields["projectIds"]
        if not isinstance(project_ids, list) or not project_ids:
            raise CliError(
                "projectIds must be a nonempty array of UUIDs", error_code="INVALID_INPUT"
            )
        project_ids = [_validate_admin_uuid(pid, "projectIds") for pid in project_ids]
        if len(set(project_ids)) != len(project_ids):
            raise CliError("projectIds must be unique", error_code="INVALID_INPUT")
        fields["projectIds"] = project_ids
        if project_ids and not set(project_ids) <= set(list_projects(include_archived=True)):
            raise CliError(
                "projectIds must belong to accessible account projects", error_code="INVALID_INPUT"
            )
    data = api.query(
        {
            f"milestone({json.dumps([milestone_id])})": [
                "isDeleted",
                "isGlobal",
                {"milestoneProjects": ["projectId"]},
            ]
        }
    )
    row = data.get("milestone", {}).get(milestone_id, {})
    if _get_field(row, "is_deleted", "isDeleted") is not False:
        raise CliError("Milestone must exist and be active", error_code="INVALID_INPUT")
    current = _milestone_scope(data, row)
    if fields.get("isGlobal") is True:
        return {"is_global": True}
    if current["is_global"] and "projectIds" not in fields:
        raise CliError(
            "A global milestone needs projectIds when made non-global", error_code="INVALID_INPUT"
        )
    desired = fields.get("projectIds", current["project_ids"])
    removed = set(current["project_ids"]) - set(desired)
    if (current["is_global"] or removed) and not allow_project_removal:
        raise CliError(
            "This narrows milestone scope and can remove card/deck associations. "
            "Inspect fresh metadata, then set allow_project_removal=true to acknowledge. "
            f"Removed project IDs: {sorted(removed)}",
            error_code="PROJECT_REMOVAL_REQUIRES_ACK",
        )
    return {"is_global": False, "project_ids": sorted(desired)}


def update_milestone(
    milestone_id: str, fields: dict[str, Any], *, allow_project_removal: bool = False
) -> dict[str, Any]:
    """Edit milestone metadata and verify every submitted field."""
    milestone_id = _validate_admin_uuid(milestone_id, "milestone_id")
    allowed = {"name", "description", "color", "date", "startDate", "isGlobal", "projectIds"}
    if not isinstance(fields, dict) or not fields or fields.keys() - allowed:
        raise CliError(
            "Provide a nonempty object with name, description, color, date, startDate, isGlobal or projectIds",
            error_code="INVALID_INPUT",
            retryable=False,
        )
    if type(allow_project_removal) is not bool:
        raise CliError("allow_project_removal must be a boolean", error_code="INVALID_INPUT")
    fields = dict(fields)
    for key, maximum in (("name", 500), ("description", 10000)):
        if key in fields and (
            not isinstance(fields[key], str)
            or len(fields[key]) > maximum
            or (key == "name" and not fields[key].strip())
        ):
            raise CliError(
                f"{key} must be a string of at most {maximum} characters; name cannot be empty",
                error_code="INVALID_INPUT",
                retryable=False,
            )
    if "color" in fields and (
        not isinstance(fields["color"], str) or fields["color"] not in VALID_MILESTONE_COLORS
    ):
        raise CliError("Invalid milestone color", error_code="INVALID_INPUT", retryable=False)
    for key in ("date", "startDate"):
        if key not in fields or (key == "startDate" and fields[key] is None):
            continue
        try:
            value = fields[key]
            if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError:
            raise CliError(
                f"{key} must be a calendar date in YYYY-MM-DD format",
                error_code="INVALID_INPUT",
                retryable=False,
            ) from None
    scope = (
        _prepare_milestone_scope(milestone_id, fields, allow_project_removal)
        if fields.keys() & {"isGlobal", "projectIds"}
        else None
    )
    read_fields: list[Any] = [key for key in fields if key not in {"isGlobal", "projectIds"}]
    if scope is not None:
        read_fields += ["isGlobal", {"milestoneProjects": ["projectId"]}]
    try:
        api.dispatch("milestones/update", {"id": milestone_id, **fields})
        try:
            data = api.query({f"milestone({json.dumps([milestone_id])})": read_fields})
            row = data.get("milestone", {}).get(milestone_id, {})
            if isinstance(row, dict) and "start_date" in row:
                row = {**row, "startDate": row["start_date"]}
            if not isinstance(row, dict) or any(
                key not in row or row[key] != value
                for key, value in fields.items()
                if key not in {"isGlobal", "projectIds"}
            ):
                raise CliError("Milestone read-back does not match the requested fields")
            if scope is not None:
                from codecks_cli.cards import _milestone_scope

                actual_scope = _milestone_scope(data, row)
                if any(actual_scope[key] != value for key, value in scope.items()):
                    raise CliError("Milestone scope read-back does not match")
        except (CliError, AttributeError, TypeError) as error:
            raise CliError(
                "Milestone update was submitted but could not be verified. Read its metadata before retrying.",
                error_code="MILESTONE_UPDATE_UNVERIFIED",
                retryable=False,
            ) from error
    finally:
        config._cache.pop("milestone_records", None)
        if scope is not None:
            config._cache.pop("decks", None)
    return {
        "ok": True,
        "milestone_id": milestone_id,
        "updated_fields": list(fields),
        "source": "api",
    }


def archive_deck(deck_name: str, project: str | None = None) -> dict[str, Any]:
    """Delete a deck via dispatch API.

    Note: Codecks dispatch API supports decks/delete but not a separate
    archive action. This deletes the deck (cards are preserved).

    Args:
        deck_name: Name of the deck to delete.
        project: Optional project name to scope deck resolution.

    Returns:
        Dict with ok, deck_name, and source.
    """
    deck_id = _resolve_deck_id(deck_name, project=project)
    payload = {"id": deck_id}

    try:
        api.dispatch("decks/delete", payload)
        return {"ok": True, "deck_name": deck_name, "source": "api"}
    except CliError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_code": e.error_code or "CLI_ERROR",
            "retryable": e.retryable,
        }
