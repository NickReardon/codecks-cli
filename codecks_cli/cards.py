"""
Card CRUD operations, hand management, conversations, and helper functions
for codecks-cli.
"""

import json
import re
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from codecks_cli import config
from codecks_cli._utils import (  # noqa: F401 — re-exported for existing consumers
    _get_field,
    _parse_date,
    _parse_iso_timestamp,
    _parse_multi_value,
    get_card_tags,
)
from codecks_cli.api import _try_call, query, session_request
from codecks_cli.exceptions import CliError

# ---------------------------------------------------------------------------
# Config helpers (.env name mappings)
# ---------------------------------------------------------------------------


def _load_env_mapping(env_key):
    """Load an id=Name mapping from a comma-separated .env value.
    Format: id1=Name1,id2=Name2"""
    mapping = {}
    raw = config.env.get(env_key, "")
    for pair in raw.split(","):
        pair = pair.strip()
        if "=" in pair:
            k, name = pair.split("=", 1)
            mapping[k.strip()] = name.strip()
    return mapping


def _load_project_records():
    """Read current active projects, including projects without decks."""
    result = query({"_root": [{"account": [{"projects": ["id", "name"]}]}]})
    return {p["id"]: p for p in result.get("project", {}).values() if p.get("id")}


def _load_project_aliases():
    """Keep explicit aliases separate from legacy setup-generated mappings."""
    try:
        aliases = json.loads(config.env.get("CODECKS_PROJECT_ALIASES", "{}"))
    except (ValueError, TypeError) as exc:
        raise CliError(
            "CODECKS_PROJECT_ALIASES must be a JSON object", error_code="INVALID_INPUT"
        ) from exc
    if not isinstance(aliases, dict) or any(
        not isinstance(pid, str) or not pid.strip() or not isinstance(name, str) or not name.strip()
        for pid, name in aliases.items()
    ):
        raise CliError(
            "Project aliases must map project IDs to nonempty names", error_code="INVALID_INPUT"
        )
    return aliases


def load_project_names():
    records = _load_project_records()
    aliases = _load_project_aliases()
    legacy = _load_env_mapping("CODECKS_PROJECTS")
    return {
        pid: aliases.get(pid) or p.get("name") or legacy.get(pid) or pid
        for pid, p in records.items()
    }


def resolve_project_id(project_name):
    """Resolve an exact ID, API name or alias without choosing between collisions."""
    if not isinstance(project_name, str) or not project_name.strip():
        return None
    records = _load_project_records()
    if project_name in records:
        return project_name
    aliases = _load_project_aliases()
    legacy = _load_env_mapping("CODECKS_PROJECTS")
    matches = [
        pid
        for pid, p in records.items()
        if project_name.lower()
        in {
            (p.get("name") or legacy.get(pid) or pid).lower(),
            aliases.get(pid, "").lower(),
        }
    ]
    if len(matches) > 1:
        raise CliError(
            f"Project '{project_name}' is ambiguous. Use a project ID: {', '.join(sorted(matches))}",
            error_code="INVALID_INPUT",
            retryable=False,
        )
    return matches[0] if matches else None


def canonical_project_name(project):
    """Map an ID, API name or alias to the display name cards carry.

    Raises INVALID_INPUT when ambiguous. Unknown names, or project lookups that
    fail for other reasons, fall back to the input so name filters still work.
    """
    if not project:
        return project
    try:
        pid = resolve_project_id(project)
        return load_project_names().get(pid, project) if pid else project
    except CliError as e:
        if e.error_code == "INVALID_INPUT":
            raise
        # ponytail: cards carry names, not IDs; unaliased duplicate names still merge.
        return project


def _milestone_scope(data, row):
    """Decode a milestone's explicitly returned project-link relation."""
    if not isinstance(data, dict) or not isinstance(row, dict):
        raise CliError("Invalid milestone scope response", error_code="INVALID_QUERY")
    is_global = _get_field(row, "is_global", "isGlobal")
    links = _get_field(row, "milestone_projects", "milestoneProjects")
    table = data.get("milestoneProject", {})
    if type(is_global) is not bool or not isinstance(links, list):
        raise CliError("Missing milestone scope", error_code="INVALID_QUERY")
    if not isinstance(table, dict) or any(not isinstance(link, str) for link in links):
        raise CliError("Invalid milestone project links", error_code="INVALID_QUERY")
    projects = []
    for link in links:
        record = table.get(link, {})
        if not isinstance(record, dict):
            raise CliError("Invalid milestone project link", error_code="INVALID_QUERY")
        project_id = _get_field(record, "project_id", "projectId")
        try:
            projects.append(str(uuid.UUID(project_id)))
        except (ValueError, TypeError, AttributeError):
            raise CliError("Invalid milestone project link", error_code="INVALID_QUERY") from None
    if len(set(projects)) != len(projects):
        raise CliError("Duplicate milestone project links", error_code="INVALID_QUERY")
    return {"is_global": is_global, "project_ids": sorted(projects)}


def _load_milestone_records(*, include_metadata=False):
    """API records own milestone identity; metadata reads always bypass the cache."""
    if not include_metadata and "milestone_records" in config._cache:
        return config._cache["milestone_records"]
    fields: list[Any] = ["id", "name", "isDeleted"]
    if include_metadata:
        fields += [
            "description",
            "color",
            "date",
            "startDate",
            "isGlobal",
            {"milestoneProjects": ["projectId"]},
        ]
    result = query({"_root": [{"account": [{"milestones": fields}]}]})
    rows = result.get("milestone", {})
    if not isinstance(rows, dict) or any(not isinstance(row, dict) for row in rows.values()):
        raise CliError(
            "Invalid milestone records from the API", error_code="INVALID_QUERY", retryable=False
        )
    records = {
        row.get("id", key): row
        for key, row in rows.items()
        if not _get_field(row, "is_deleted", "isDeleted")
    }
    if include_metadata:
        records = {mid: {**row, **_milestone_scope(result, row)} for mid, row in records.items()}
    config._cache["milestone_records"] = records
    return records


def load_milestone_names():
    manual = _load_env_mapping("CODECKS_MILESTONES")
    return {
        mid: row.get("name") or manual.get(mid) or mid
        for mid, row in _load_milestone_records().items()
    }


def load_users():
    """Load user ID->name mapping from account roles. Cached per invocation."""
    if "users" in config._cache:
        return config._cache["users"]
    result = _try_call(
        query, {"_root": [{"account": [{"roles": ["userId", "role", {"user": ["id", "name"]}]}]}]}
    )
    user_map = {}
    if result:
        for uid, udata in result.get("user", {}).items():
            user_map[uid] = udata.get("name", "")
    config._cache["users"] = user_map
    return user_map


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def _filter_cards(result, predicate):
    """Filter result['card'] dict by predicate(key, card). Returns result."""
    result["card"] = {k: v for k, v in result.get("card", {}).items() if predicate(k, v)}
    return result


def get_account():
    q = {"_root": [{"account": ["name", "id"]}]}
    return query(q)


def _get_active_project_ids():
    """Return a set of project IDs that are currently active.
    Uses account.projects (active only) as a whitelist — excludes archived and deleted projects."""
    return set(load_project_names())


def list_decks(*, include_metadata=False):
    if not include_metadata and "decks" in config._cache:
        return config._cache["decks"]
    fields = ["title", "id", "projectId", "isDeleted"]
    if include_metadata:
        fields += ["description", "deckType", "defaultCard"]
    q = {"_root": [{"account": [{"decks": fields}]}]}
    result = query(q)

    # Whitelist: only keep decks from active projects (excludes archived and deleted projects).
    # Falls back gracefully if the projects query fails — returns all non-deleted decks.
    active_pids = _get_active_project_ids()
    result["deck"] = {
        k: v
        for k, v in result.get("deck", {}).items()
        if not _get_field(v, "is_deleted", "isDeleted")
        and (not active_pids or _get_field(v, "project_id", "projectId") in active_pids)
    }

    if not include_metadata:
        config._cache["decks"] = result
    return result


_FIELDS_MINIMAL = [
    "title",
    "status",
    "priority",
    "deckId",
    "effort",
    {"assignee": ["name", "id"]},
]

# isDoc is part of the list field set (not just the full one) so that
# cache-warming calls (include_content=False) can still tell doc cards apart —
# the MCP doc-card guardrail reads it off cached cards.
_FIELDS_LIST = _FIELDS_MINIMAL + [
    "masterTags",
    "lastUpdatedAt",
    "isDoc",
    # childCardInfo comes back as "{}" for hero cards; the aggregate is exact (v2.96).
    # In the list set so cached cards (MCP) can tell hero cards apart too.
    "count:childCards",
]

_FIELDS_FULL = _FIELDS_LIST + [
    "createdAt",
    "milestoneId",
    "content",
]


def list_cards(
    deck_filter=None,
    status_filter=None,
    project_filter=None,
    search_filter=None,
    milestone_filter=None,
    tag_filter=None,
    owner_filter=None,
    priority_filter=None,
    stale_days=None,
    updated_after=None,
    updated_before=None,
    archived=False,
    include_content=True,
):
    if include_content:
        card_fields = list(_FIELDS_FULL)
    else:
        # Content needed for search — upgrade to full if searching
        if search_filter:
            card_fields = list(_FIELDS_FULL)
        else:
            card_fields = list(_FIELDS_LIST)
    card_query: dict[str, object] = {"visibility": "archived" if archived else "default"}
    # NOTE: Codecks GraphQL API does not support server-side limit/offset
    # on card queries. All pagination is client-side. Tested 2026-03.

    # Parse and validate status filter (supports comma-separated values)
    status_values = None
    if status_filter:
        status_values = _parse_multi_value(status_filter, config.VALID_STATUSES, "status")
        if len(status_values) == 1:
            # Single value → server-side filter
            card_query["status"] = status_values[0]
            status_values = None  # no client-side filter needed
        elif len(status_values) > 1:
            # Multi-value → use 'in' operator for server-side filter
            card_query["status"] = {"in": list(status_values)}
            status_values = None  # server handled it

    # Resolve deck filter
    if deck_filter:
        decks_result = list_decks()
        deck_id = None
        for _key, deck in decks_result.get("deck", {}).items():
            if deck.get("title", "").lower() == deck_filter.lower():
                deck_id = deck.get("id")
                break
        if deck_id:
            card_query["deckId"] = deck_id
        else:
            raise CliError(f"[ERROR] Deck '{deck_filter}' not found.")

    q = {"_root": [{"account": [{f"cards({json.dumps(card_query)})": card_fields}]}]}
    result = query(q)

    # Client-side multi-value status filter (when >1 status specified)
    if status_values:
        status_set = set(status_values)
        _filter_cards(result, lambda k, c: c.get("status") in status_set)

    # Client-side priority filter (supports comma-separated values)
    if priority_filter:
        pri_values = _parse_multi_value(priority_filter, config.VALID_PRIORITIES, "priority")
        # Normalize "null" → match cards with None priority
        pri_set = set(pri_values)
        has_null = "null" in pri_set
        pri_set.discard("null")
        _filter_cards(
            result,
            lambda k, c: c.get("priority") in pri_set or (has_null and not c.get("priority")),
        )

    # Client-side project filter (cards don't have projectId directly)
    if project_filter:
        decks_result = list_decks()
        project_deck_ids = get_project_deck_ids(decks_result, project_filter)
        if project_deck_ids is None:
            available = [n for n in load_project_names().values()]
            hint = f" Available: {', '.join(available)}" if available else ""
            raise CliError(f"[ERROR] Project '{project_filter}' not found.{hint}")
        _filter_cards(result, lambda k, c: _get_field(c, "deck_id", "deckId") in project_deck_ids)

    # Client-side text search
    if search_filter:
        search_lower = search_filter.lower()
        _filter_cards(
            result,
            lambda k, c: (
                search_lower in (c.get("title", "") or "").lower()
                or search_lower in (c.get("content", "") or "").lower()
            ),
        )

    # Client-side milestone filter
    if milestone_filter:
        milestone_id = resolve_milestone_id(milestone_filter)
        _filter_cards(
            result, lambda k, c: _get_field(c, "milestone_id", "milestoneId") == milestone_id
        )

    # Client-side tag filter
    if tag_filter:
        tag_lower = tag_filter.lower()
        _filter_cards(result, lambda k, c: any(t.lower() == tag_lower for t in get_card_tags(c)))

    # Client-side owner filter
    if owner_filter:
        owner_lower = owner_filter.lower()
        # Special case: "none" finds unassigned cards
        if owner_lower == "none":
            _filter_cards(result, lambda k, c: not c.get("assignee"))
        else:
            # Resolve owner name to user ID
            users = result.get("user", {})
            owner_id = None
            for uid, udata in users.items():
                if (udata.get("name") or "").lower() == owner_lower:
                    owner_id = uid
                    break
            if owner_id is None:
                user_map = load_users()
                for uid, name in user_map.items():
                    if name.lower() == owner_lower:
                        owner_id = uid
                        break
            if owner_id is None:
                available = [u.get("name", "") for u in result.get("user", {}).values()]
                if not available:
                    available = list(load_users().values())
                hint = f" Available: {', '.join(available)}" if available else ""
                raise CliError(f"[ERROR] Owner '{owner_filter}' not found.{hint}")
            _filter_cards(result, lambda k, c: c.get("assignee") == owner_id)

    # Client-side date filters
    # Cards with missing timestamps are excluded from all date-filtered results.
    if stale_days is not None:
        cutoff = datetime.now(UTC) - timedelta(days=stale_days)

        def _stale_pred(k, c):
            ts = _parse_iso_timestamp(_get_field(c, "last_updated_at", "lastUpdatedAt"))
            return ts is not None and ts < cutoff

        _filter_cards(result, _stale_pred)

    if updated_after:
        after_dt = _parse_date(updated_after)

        def _after_pred(k, c):
            ts = _parse_iso_timestamp(_get_field(c, "last_updated_at", "lastUpdatedAt"))
            return ts is not None and ts >= after_dt

        _filter_cards(result, _after_pred)

    if updated_before:
        before_dt = _parse_date(updated_before)

        def _before_pred(k, c):
            ts = _parse_iso_timestamp(_get_field(c, "last_updated_at", "lastUpdatedAt"))
            return ts is not None and ts < before_dt

        _filter_cards(result, _before_pred)

    return result


def get_project_deck_ids(decks_result, project_name):
    """Return deck IDs for one unambiguous project, or None if unknown."""
    pid = resolve_project_id(project_name)
    if pid is None:
        return None
    return {
        deck.get("id")
        for deck in decks_result.get("deck", {}).values()
        if _get_field(deck, "project_id", "projectId") == pid
    }


def _build_project_map(decks_result):
    """Build a map of projectId -> {name, deck_ids} from deck data.
    Project names come from current API records with optional local aliases."""
    project_names = load_project_names()
    project_decks: dict[str, dict] = {}
    for _key, deck in decks_result.get("deck", {}).items():
        pid = _get_field(deck, "project_id", "projectId")
        if pid:
            if pid not in project_decks:
                project_decks[pid] = {"deck_ids": set(), "deck_titles": []}
            project_decks[pid]["deck_ids"].add(deck.get("id"))
            project_decks[pid]["deck_titles"].append(deck.get("title", ""))

    # Include active projects without decks.
    for pid, pname in project_names.items():
        if pid not in project_decks:
            project_decks[pid] = {"deck_ids": set(), "deck_titles": [], "name": pname}

    # Apply display names, falling back to the project ID.
    for pid, info in project_decks.items():
        if "name" not in info:
            info["name"] = project_names.get(pid, pid)

    return project_decks


def get_card(card_id, *, archived=False, minimal=False):
    visibility = "archived" if archived else "default"
    card_filter = json.dumps({"cardId": card_id, "visibility": visibility})
    if minimal:
        # Reduced field set for sub-cards that may 500 with full fields.
        # Drops checkboxStats and parentCard which can trigger API errors.
        card_fields = [
            "title",
            "status",
            "priority",
            "content",
            "deckId",
            "effort",
            "createdAt",
            "milestoneId",
            "masterTags",
            "lastUpdatedAt",
            "isDoc",
            {"assignee": ["name", "id"]},
            {"childCards": ["title", "status"]},
        ]
    else:
        card_fields = [
            "title",
            "status",
            "priority",
            "content",
            "deckId",
            "effort",
            "createdAt",
            "milestoneId",
            "masterTags",
            "lastUpdatedAt",
            "isDoc",
            "checkboxStats",
            {"assignee": ["name", "id"]},
            {"parentCard": ["title"]},
            {"childCards": ["title", "status"]},
            {
                "resolvables": [
                    "context",
                    "isClosed",
                    "createdAt",
                    {"creator": ["name"]},
                    {"entries": ["content", "createdAt", {"author": ["name"]}]},
                ]
            },
        ]
    q = {"_root": [{"account": [{f"cards({card_filter})": card_fields}]}]}
    return query(q)


def list_tags():
    """Query project-level tags (masterTags) from the account."""
    q = {"_root": [{"account": [{"masterTags": ["title", "id", "color", "emoji"]}]}]}
    return query(q)


def list_milestones(*, include_metadata=False):
    """List API milestones, using local names only for unnamed API records."""
    records = _load_milestone_records(include_metadata=include_metadata)
    manual = _load_env_mapping("CODECKS_MILESTONES")
    output = {}
    for mid, row in records.items():
        info = {"name": row.get("name") or manual.get(mid) or mid, "cards": []}
        if include_metadata:
            for key in ("description", "color", "date"):
                if key not in row:
                    raise CliError(
                        "Missing milestone metadata", error_code="INVALID_QUERY", retryable=False
                    )
                info[key] = row[key]
            if "start_date" not in row and "startDate" not in row:
                raise CliError(
                    "Missing milestone start date", error_code="INVALID_QUERY", retryable=False
                )
            info["start_date"] = _get_field(row, "start_date", "startDate")
            info.update({key: row[key] for key in ("is_global", "project_ids")})
        output[mid] = info
    return output


def list_activity(limit=20):
    """Query recent account activity."""
    q = {
        "_root": [
            {
                "account": [
                    {
                        "activities": [
                            "type",
                            "createdAt",
                            "data",
                            {"card": ["title"]},
                            {"changer": ["name"]},
                            {"deck": ["title"]},
                        ]
                    }
                ]
            }
        ]
    }
    result = query(q)
    activities = result.get("activity", {})
    if len(activities) > limit:
        result["activity"] = dict(list(activities.items())[:limit])

    # Strip the bulky account entity and orphaned entity refs.
    # The API returns the full account object (~100K) plus entity refs for
    # ALL activities. After trimming to `limit`, strip everything that isn't
    # directly referenced by the kept activities.
    result.pop("account", None)
    kept_activities = result.get("activity", {})
    if kept_activities:
        referenced_ids: set[str] = set()
        for act in kept_activities.values():
            for ref_key in ("card", "changer", "deck"):
                ref = act.get(ref_key)
                if isinstance(ref, str):
                    referenced_ids.add(ref)
        for entity_key in ("card", "user", "deck"):
            if entity_key in result:
                result[entity_key] = {
                    k: v for k, v in result[entity_key].items() if k in referenced_ids
                }
    return result


def list_projects(*, include_archived=False):
    """List API projects, optionally including archived projects."""
    if type(include_archived) is not bool:
        raise CliError("include_archived must be a boolean", error_code="INVALID_INPUT")
    fields = ["id", "name", "visibility", {"decks": ["title", "isDeleted"]}]
    relations = [{"projects": fields}]
    if include_archived:
        relations.append({"archivedProjects": fields})
    data = query({"_root": [{"account": relations}]})
    names = _load_env_mapping("CODECKS_PROJECTS")
    output = {}
    for pid, info in data.get("project", {}).items():
        visibility = info.get("visibility", "default")
        if visibility == "deleted" or (visibility == "archived" and not include_archived):
            continue
        decks = [data.get("deck", {}).get(did, {}) for did in info.get("decks", [])]
        titles = [
            d.get("title", "") for d in decks if d and not _get_field(d, "is_deleted", "isDeleted")
        ]
        output[pid] = {
            "name": info.get("name") or names.get(pid) or pid,
            "visibility": visibility,
            "deck_count": len(titles),
            "decks": titles,
        }
    return output


# ---------------------------------------------------------------------------
# Enrichment (resolve IDs to human-readable names)
# ---------------------------------------------------------------------------


def enrich_cards(cards_dict, user_data=None):
    """Add deck_name, milestone_name, owner_name to card dicts."""
    decks_result = list_decks()
    deck_names = {}
    for _key, deck in decks_result.get("deck", {}).items():
        deck_names[deck.get("id")] = deck.get("title", "")

    milestone_names = (
        load_milestone_names()
        if any(_get_field(card, "milestone_id", "milestoneId") for card in cards_dict.values())
        else {}
    )

    # Build user name map from user_data (query result) or load_users()
    user_names = {}
    if user_data:
        for uid, udata in user_data.items():
            user_names[uid] = udata.get("name", "")
    if not user_names:
        user_names = load_users()

    for _key, card in cards_dict.items():
        did = _get_field(card, "deck_id", "deckId")
        if did:
            card["deck_name"] = deck_names.get(did, did)
        mid = _get_field(card, "milestone_id", "milestoneId")
        if mid:
            card["milestone_name"] = milestone_names.get(mid, mid)
        # Resolve owner name
        assignee = card.get("assignee")
        if assignee:
            card["owner_name"] = user_names.get(assignee, assignee)
        # Normalize tags field
        card["tags"] = get_card_tags(card)
        # Sub-card count (only present when the query asked for it)
        if "count:childCards" in card:
            card["sub_card_count"] = card.pop("count:childCards") or 0

    return cards_dict


def compute_card_stats(cards_dict):
    """Compute summary statistics from card data."""
    stats: dict = {
        "total": len(cards_dict),
        "by_status": {},
        "by_priority": {},
        "by_deck": {},
        "by_owner": {},
    }
    total_effort = 0
    effort_count = 0
    for _key, card in cards_dict.items():
        status = card.get("status", "unknown")
        stats["by_status"][status] = stats["by_status"].get(status, 0) + 1

        priority = card.get("priority") or "none"
        stats["by_priority"][priority] = stats["by_priority"].get(priority, 0) + 1

        deck = card.get("deck_name", card.get("deck_id", "unknown"))
        stats["by_deck"][deck] = stats["by_deck"].get(deck, 0) + 1

        owner = card.get("owner_name") or "unassigned"
        stats["by_owner"][owner] = stats["by_owner"].get(owner, 0) + 1

        effort = card.get("effort")
        if effort is not None:
            total_effort += effort
            effort_count += 1

    stats["total_effort"] = total_effort
    stats["avg_effort"] = round(total_effort / effort_count, 1) if effort_count else 0
    return stats


# ---------------------------------------------------------------------------
# Mutation helpers
# ---------------------------------------------------------------------------


def create_card(title, content=None, deck_id=None, **fields):
    """Create a card via the official API (``dispatch/cards/create``).
    First line of content becomes the card title.

    Without ``deck_id`` the card goes to the CODECKS_DEFAULT_DECK deck; the API
    itself would create a private, deck-less card. Extra ``fields`` (priority,
    effort, assigneeId, milestoneId, masterTags) are sent in the same request.

    If ``content`` already begins with the title (followed by newline) or
    equals it exactly, it is used as-is rather than re-prepended — this
    prevents accidental title duplication when callers pass back content
    that already includes the title-echo line.
    """
    if content:
        if content.startswith(title + "\n") or content == title:
            full_content = content
        else:
            full_content = title + "\n\n" + content
    else:
        full_content = title
    payload = {
        "content": full_content,
        "deckId": deck_id or default_deck_id(),
        "assigneeId": None,
        "milestoneId": None,
        "masterTags": [],
        "attachments": [],
    }
    payload.update(fields)
    result = session_request("/dispatch/cards/create", payload)
    card_id = (result.get("payload") or {}).get("id", "")
    return {"cardId": card_id}


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def default_deck_id():
    """Return the CODECKS_DEFAULT_DECK deck ID, or explain how to set it.

    Setup and ``default-deck`` store the deck ID (unambiguous across projects,
    survives renames); a hand-written deck name is still resolved."""
    value = config.DEFAULT_DECK
    if not value:
        raise CliError(
            "[ERROR] No default deck set, so the card has nowhere to go. "
            "Run: codecks-cli default-deck <deck name>  (or pass --deck)"
        )
    return value if _UUID_RE.match(value) else resolve_deck_id(value)


def update_card(card_id, **kwargs):
    """Update card properties via dispatch (uses API token).
    Supported fields: status, priority, effort, deckId, title, content,
    milestoneId, parentCardId, assigneeId, masterTags, isDoc.
    None values are sent as JSON null to clear fields."""
    payload = {"id": card_id}
    payload.update(kwargs)
    return session_request("/dispatch/cards/update", payload)


def archive_card(card_id):
    """Archive a card (uses API token)."""
    return session_request(
        "/dispatch/cards/update",
        {
            "id": card_id,
            "visibility": "archived",
        },
    )


def unarchive_card(card_id):
    """Unarchive a card (uses API token)."""
    return session_request(
        "/dispatch/cards/update",
        {
            "id": card_id,
            "visibility": "default",
        },
    )


def delete_card(card_id):
    """Delete a card — archives first, then deletes (uses API token)."""
    archive_card(card_id)
    try:
        return session_request(
            "/dispatch/cards/bulkUpdate",
            {
                "ids": [card_id],
                "visibility": "deleted",
                "deleteFiles": False,
            },
        )
    except CliError:
        print(
            f"Warning: Card {card_id} was archived but delete failed. Use 'unarchive' to recover.",
            file=sys.stderr,
        )
        raise


def bulk_status(card_ids, status):
    """Update status for multiple cards at once."""
    return session_request(
        "/dispatch/cards/bulkUpdate",
        {
            "ids": card_ids,
            "status": status,
        },
    )


# ---------------------------------------------------------------------------
# Hand helpers (personal card queue)
# ---------------------------------------------------------------------------


# The token's user, looked up once per process (survives config._cache.clear()).
_looked_up_user_id = ""


def _get_user_id():
    """Return the user whose hand/comments this token acts on.

    Personal tokens (cdxut_) always use the API's loggedInUser, so a stale
    CODECKS_USER_ID can't point at a teammate. Organization tokens (cdxat_) have no
    user: CODECKS_USER_ID, else the first account owner. API errors propagate: a
    429 or 503 must not silently switch to the owner.
    """
    global _looked_up_user_id
    if _looked_up_user_id:
        return _looked_up_user_id
    token = config.SESSION_TOKEN or ""
    if not token.startswith("cdxut_") and config.USER_ID:
        return config.USER_ID
    uid = None
    if not token.startswith("cdxat_"):
        # session_request, not query(): query() drops the "_root" key this answer lives in.
        me = session_request("/", {"query": {"_root": [{"loggedInUser": ["id"]}]}}, idempotent=True)
        uid = ((me or {}).get("_root") or {}).get("loggedInUser")
    if not (isinstance(uid, str) and uid):
        if config.USER_ID and not token.startswith("cdxut_"):
            return config.USER_ID
        uid = _first_owner_id()
    _looked_up_user_id = uid
    return uid


def _first_owner_id():
    """First account owner (else first member): the fallback user for org tokens."""
    result = query({"_root": [{"account": [{"roles": ["userId", "role"]}]}]})
    roles = list((result.get("accountRole") or {}).values())
    for entry in sorted(roles, key=lambda e: e.get("role") != "owner"):
        uid = _get_field(entry, "user_id", "userId")
        if uid:
            return uid
    raise CliError("[ERROR] Could not determine your user ID. Run: py codecks_api.py setup")


def list_hand():
    """Query the current user's hand (queueEntries).

    account.queueEntries holds every member's hand, so filter to our own user.
    """
    entries = f"queueEntries({json.dumps({'userId': _get_user_id()})})"
    q = {"_root": [{"account": [{entries: ["card", "sortIndex", "user"]}]}]}
    return query(q)


def extract_hand_card_ids(hand_result):
    """Extract card IDs from a list_hand() result as a set."""
    hand_card_ids = set()
    for entry in (hand_result.get("queueEntry") or {}).values():
        cid = entry.get("card") or entry.get("cardId")
        if cid:
            hand_card_ids.add(cid)
    return hand_card_ids


def add_to_hand(card_ids):
    """Add cards to the current user's hand."""
    user_id = _get_user_id()
    return session_request(
        "/dispatch/handQueue/setCardOrders",
        {
            "sessionId": str(uuid.uuid4()),
            "userId": user_id,
            "cardIds": card_ids,
            "draggedCardIds": card_ids,
        },
    )


def remove_from_hand(card_ids):
    """Remove cards from the current user's hand."""
    return session_request(
        "/dispatch/handQueue/removeCards",
        {
            "sessionId": str(uuid.uuid4()),
            "cardIds": card_ids,
        },
    )


# ---------------------------------------------------------------------------
# Conversation helpers (threaded comments on cards)
# ---------------------------------------------------------------------------


def create_comment(card_id, content):
    """Create a new comment thread on a card."""
    user_id = _get_user_id()
    return session_request(
        "/dispatch/resolvables/create",
        {
            "cardId": card_id,
            "userId": user_id,
            "content": content,
            "context": "comment",
        },
    )


def reply_comment(resolvable_id, content):
    """Reply to an existing comment thread."""
    user_id = _get_user_id()
    return session_request(
        "/dispatch/resolvables/comment",
        {
            "resolvableId": resolvable_id,
            "content": content,
            "authorId": user_id,
        },
    )


def close_comment(resolvable_id, card_id):
    """Close a comment thread."""
    user_id = _get_user_id()
    return session_request(
        "/dispatch/resolvables/close",
        {
            "id": resolvable_id,
            "isClosed": True,
            "cardId": card_id,
            "closedBy": user_id,
        },
    )


def reopen_comment(resolvable_id, card_id):
    """Reopen a closed comment thread."""
    return session_request(
        "/dispatch/resolvables/reopen",
        {
            "id": resolvable_id,
            "isClosed": False,
            "cardId": card_id,
        },
    )


def get_conversations(card_id):
    """Fetch all conversations (resolvables) on a card."""
    card_filter = json.dumps({"cardId": card_id, "visibility": "default"})
    q = {
        "_root": [
            {
                "account": [
                    {
                        f"cards({card_filter})": [
                            "title",
                            {
                                "resolvables": [
                                    "context",
                                    "isClosed",
                                    "createdAt",
                                    {"creator": ["name"]},
                                    {"entries": ["content", "createdAt", {"author": ["name"]}]},
                                ]
                            },
                        ]
                    }
                ]
            }
        ]
    }
    return query(q)


# ---------------------------------------------------------------------------
# Name -> ID resolution helpers
# ---------------------------------------------------------------------------


def _find_closest(query: str, candidates: list[str]) -> str | None:
    """Find closest matching string by prefix then substring."""
    q = query.lower()
    for c in candidates:
        if c.lower().startswith(q):
            return c
    for c in candidates:
        if q in c.lower():
            return c
    return None


def resolve_deck_id(deck_name, project=None):
    """Resolve deck name to ID with fuzzy match suggestions.

    Args:
        deck_name: Deck name (case-insensitive).
        project: Optional project name to scope resolution. When provided,
            only decks belonging to that project are considered, preventing
            cross-project name collisions.
    """
    decks_result = list_decks()

    # Resolve project name to ID if scoping is requested
    project_id = None
    if project is not None:
        project_id = resolve_project_id(project)
        if project_id is None:
            raise CliError(f"[ERROR] Project '{project}' not found for deck resolution.")

    available = []
    for _key, deck in decks_result.get("deck", {}).items():
        # Skip decks outside the target project when scoped
        if project_id is not None and _get_field(deck, "project_id", "projectId") != project_id:
            continue
        title = deck.get("title", "")
        if title.lower() == deck_name.lower():
            return deck.get("id")
        available.append(title)
    closest = _find_closest(deck_name, available)
    hint = f" Did you mean '{closest}'?" if closest else ""
    avail_str = f" Available: {', '.join(sorted(available))}" if available else ""
    scope = f" in project '{project}'" if project else ""
    raise CliError(f"[ERROR] Deck '{deck_name}' not found{scope}.{hint}{avail_str}")


def resolve_milestone_id(milestone_name):
    """Resolve an API-backed name; reject ambiguous names and stale local aliases."""
    names = load_milestone_names()
    matches = [mid for mid, name in names.items() if name.lower() == milestone_name.lower()]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise CliError(
            f"Milestone '{milestone_name}' is ambiguous",
            error_code="INVALID_INPUT",
            retryable=False,
        )
    available = ", ".join(names.values())
    raise CliError(
        f"Milestone '{milestone_name}' not found. Available: {available}",
        error_code="INVALID_INPUT",
        retryable=False,
    )
