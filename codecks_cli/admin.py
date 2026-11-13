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

    from codecks_cli.cards import resolve_project_id

    pid = resolve_project_id(project_name)
    if pid is None:
        raise CliError(f"[ERROR] Project '{project_name}' not found.", error_code="INVALID_INPUT")
    return str(pid)


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


def _read_deck_milestone(deck_id, milestones, extra_fields=()):
    keys = {
        mid: "count:cards(" + json.dumps({"visibility": "default", "milestoneId": mid}) + ")"
        for mid in milestones
    }
    total_key = 'count:cards({"visibility": "default"})'
    data = api.query(
        {
            f"deck({json.dumps([deck_id])})": [
                "milestoneId",
                "projectId",
                "isDeleted",
                total_key,
                *keys.values(),
                *extra_fields,
            ]
        }
    )
    try:
        row = data["deck"][deck_id]
        if not isinstance(row, dict) or _get_field(row, "is_deleted", "isDeleted") is not False:
            raise ValueError("Deck is missing or deleted")
        project = _validate_admin_uuid(_get_field(row, "project_id", "projectId"), "projectId")
        if "milestoneId" not in row and "milestone_id" not in row:
            raise ValueError("Missing deck milestone")
        milestone = _get_field(row, "milestone_id", "milestoneId")
        if milestone is not None:
            milestone = _validate_admin_uuid(milestone, "milestoneId")
        total = row[total_key]
        counts = {mid: row[key] for mid, key in keys.items()}
        if (
            type(total) is not int
            or total < 0
            or any(type(count) is not int or not 0 <= count <= total for count in counts.values())
            or sum(counts.values()) > total
        ):
            raise ValueError("Invalid card counts")
    except (KeyError, TypeError, ValueError, CliError) as error:
        raise CliError(
            "Deck milestone metadata or card counts are incomplete",
            error_code="INVALID_QUERY",
            retryable=False,
        ) from error
    return row, project, milestone, total, counts


def update_deck(
    deck_id: str, fields: dict[str, Any], *, allow_card_changes: bool = False
) -> dict[str, Any]:
    """Update deck metadata. defaultCard is an experimental whole-template replacement."""

    def invalid(message: str) -> None:
        raise CliError(message, error_code="INVALID_INPUT", retryable=False)

    deck_id = _validate_admin_uuid(deck_id, "deck_id")
    allowed = {"title", "description", "deckType", "defaultCard", "milestoneId"}
    if not isinstance(fields, dict) or not fields or fields.keys() - allowed:
        invalid(
            "fields must be a nonempty object containing title, description, deckType, defaultCard or milestoneId"
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
    if type(allow_card_changes) is not bool:
        invalid("allow_card_changes must be a boolean")
    affected = 0
    if "milestoneId" in fields:
        fields = dict(fields)
        target = fields["milestoneId"]
        if target is not None:
            target = fields["milestoneId"] = _validate_admin_uuid(target, "milestoneId")
        counted = {None, target}
        _, project, old, total, before = _read_deck_milestone(deck_id, counted)
        if target is not None:
            from codecks_cli.cards import _milestone_scope

            data = api.query(
                {
                    f"milestone({json.dumps([target])})": [
                        "isDeleted",
                        "isGlobal",
                        {"milestoneProjects": ["projectId"]},
                    ]
                }
            )
            row = data.get("milestone", {}).get(target, {})
            if _get_field(row, "is_deleted", "isDeleted") is not False:
                invalid("Milestone must exist and be active")
            scope = _milestone_scope(data, row)
            if not scope["is_global"] and project not in scope["project_ids"]:
                invalid("Milestone does not support the deck's project")
            affected = total - before[target]
            if old == target and affected:
                invalid(
                    "Deck already uses this milestone; assigning it again does not move cards. "
                    "Update the differing card milestones directly."
                )
        elif old is not None:
            counted.add(old)
            _, _, current, total, before = _read_deck_milestone(deck_id, counted)
            if current != old:
                invalid("Deck milestone changed during preflight; read it again")
            affected = before[old]
        if affected and not allow_card_changes:
            raise CliError(
                f"This changes the milestone of {affected} active card(s). "
                f"Deck milestone: {old} -> {target}. "
                "Set allow_card_changes=true to acknowledge.",
                error_code="DECK_MILESTONE_REQUIRES_ACK",
                retryable=False,
            )
    try:
        api.dispatch("decks/update", {"id": deck_id.lower(), **fields})
        if "milestoneId" in fields:
            try:
                row, _, actual, after_total, after = _read_deck_milestone(
                    deck_id, counted, [key for key in fields if key != "milestoneId"]
                )
                verified = actual == target and after_total == total
                if target is not None:
                    verified = verified and after[target] == total
                elif old is not None:
                    verified = (
                        verified and after[old] == 0 and after[None] == before[None] + affected
                    )
                else:
                    verified = verified and after[None] == before[None]
                aliases = {"defaultCard": "default_card", "deckType": "deck_type"}
                verified = verified and all(
                    _get_field(row, aliases.get(key, key), key) == value
                    for key, value in fields.items()
                    if key != "milestoneId"
                )
                if not verified:
                    raise CliError("Deck or card milestone read-back does not match")
            except (CliError, AttributeError, TypeError) as error:
                raise CliError(
                    "Deck update was submitted, but the deck/card milestones could not be verified. "
                    "Read the deck and cards before retrying.",
                    error_code="DECK_UPDATE_UNVERIFIED",
                    retryable=False,
                ) from error
        elif "defaultCard" in fields:
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
    result = {
        "ok": True,
        "deck_id": deck_id.lower(),
        "updated_fields": list(fields),
        "source": "api",
    }
    if "milestoneId" in fields:
        result["affected_card_count"] = affected
    return result


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
