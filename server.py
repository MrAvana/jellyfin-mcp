"""
Jellyfin MCP Server
====================
Exposes a Jellyfin Media Server as a set of MCP tools, so any MCP-aware
client (n8n's MCP Client Tool node, Claude Desktop, Claude.ai connectors,
etc.) can search, filter, recommend, and remote-control playback without
the client having to know Jellyfin's REST API.

This replaces an n8n "AI agent + raw HTTP tool" setup: instead of an LLM
building Jellyfin URLs from a giant instruction prompt, each Jellyfin
operation is now a real function the model just calls with normal
arguments. All the URL-building, user/person-id lookup, and TTS-friendly
formatting live in code instead of in a prompt.

Design note on tool count vs. token usage:
Every tool's name + description + parameter schema is sent to the model
as context on EVERY agent turn (not just once). So the cost driver is
"how many tools exist", not "how many query patterns are supported". To
keep the free-form filtering the old raw-HTTP agent could improvise
(year range, watched/unwatched, favorites, rating, studio, tag, parental
rating...) without a tool-count explosion, all of it lives as optional
parameters on ONE search tool (search_titles) backed by a shared
_find_items() helper, instead of one narrow tool per filter.

Bilingual households:
This server supports the common Jellyfin setup where a household has two
user accounts against the SAME library, each configured with a different
preferred metadata language (e.g. one account shows "Project Hail Mary",
the other shows "Projet dernière chance" for the same underlying movie).
Every data-returning tool takes a `language` parameter ("en" or "fr")
that picks which of those two accounts to query, so titles, search terms,
and genres are matched and returned in the right language. See
JELLYFIN_USER_EN / JELLYFIN_USER_FR below.

Configuration (environment variables):
    JELLYFIN_URL       Base URL of the Jellyfin server (default: http://127.0.0.1:8096)
    JELLYFIN_API_KEY    Jellyfin API key (required)
    JELLYFIN_USER_EN    Name of the Jellyfin user account whose metadata
                        language is English (default: "iHome")
    JELLYFIN_USER_FR    Name of the Jellyfin user account whose metadata
                        language is French (default: "iHome French")
    MCP_TRANSPORT       stdio | sse | streamable-http (default: stdio)
    MCP_HOST            host to bind when using sse/streamable-http (default: 127.0.0.1)
    MCP_PORT            port to bind when using sse/streamable-http (default: 8000)

Run:
    python server.py                      # stdio, for local MCP clients
    MCP_TRANSPORT=streamable-http python server.py   # HTTP, for n8n / remote clients
"""

import os
import re
from datetime import datetime
from typing import Any, Optional

import httpx
from mcp.server.mcpserver import MCPServer

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://127.0.0.1:8096").rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")

if not JELLYFIN_API_KEY:
    raise RuntimeError(
        "JELLYFIN_API_KEY environment variable is required. "
        "Set it to a Jellyfin API key (Dashboard -> API Keys)."
    )

# Jellyfin 12.0 removed the legacy auth headers/params entirely (X-Emby-Token,
# X-MediaBrowser-Token, X-Emby-Authorization, and the `api_key` query param).
# The only supported method now is the `Authorization` header using the
# `MediaBrowser` scheme. Client/Device/DeviceId/Version aren't required for
# an API-key request, but sending them makes this server identifiable in the
# Jellyfin dashboard instead of showing up as a blank/unknown client.
# See: https://gist.github.com/nielsvanvelzen/ea047d9028f676185832e51ffaf12a6f
_CLIENT_NAME = os.environ.get("JELLYFIN_CLIENT_NAME", "jellyfin-mcp")
_CLIENT_VERSION = os.environ.get("JELLYFIN_CLIENT_VERSION", "1.0.0")
_DEVICE_NAME = os.environ.get("JELLYFIN_DEVICE_NAME", "jellyfin-mcp-server")
_DEVICE_ID = os.environ.get("JELLYFIN_DEVICE_ID", "jellyfin-mcp-server-001")

_AUTH_HEADER = (
    f'MediaBrowser Token="{JELLYFIN_API_KEY}", '
    f'Client="{_CLIENT_NAME}", Device="{_DEVICE_NAME}", '
    f'DeviceId="{_DEVICE_ID}", Version="{_CLIENT_VERSION}"'
)

_HEADERS = {
    "Authorization": _AUTH_HEADER,
    "Content-Type": "application/json",
}

# Maps a language code to the Jellyfin user account whose preferred
# metadata language matches it. Two accounts against the same library,
# each returning titles/genres/overviews in a different language.
_LANGUAGE_USER_NAME = {
    "en": os.environ.get("JELLYFIN_USER_EN", "iHome"),
    "fr": os.environ.get("JELLYFIN_USER_FR", "iHome French"),
}
_DEFAULT_LANGUAGE = "en"

# Cached for the lifetime of the process: the full /Users list, and the
# resolved user id per language, so we don't refetch on every tool call.
_all_users_cache: Optional[list[dict[str, Any]]] = None
_user_id_by_language: dict[str, str] = {}

_DATE_SORT_FIELDS = ("DateCreated", "PremiereDate", "CommunityRating", "DatePlayed")


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------


async def _request(method: str, path: str, params: dict[str, Any] | None = None) -> Any:
    """Call the Jellyfin API and return parsed JSON (or {} on empty body)."""
    url = f"{JELLYFIN_URL}{path}"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.request(method, url, headers=_HEADERS, params=params)
        resp.raise_for_status()
        if not resp.content:
            return {}
        return resp.json()


async def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    return await _request("GET", path, params)


async def _post(path: str, params: dict[str, Any] | None = None) -> Any:
    return await _request("POST", path, params)


async def _get_all_users() -> list[dict[str, Any]]:
    global _all_users_cache
    if _all_users_cache is None:
        _all_users_cache = await _get("/Users", {"Fields": "Name,Id"})
    return _all_users_cache


async def _resolve_user_id(language: str = _DEFAULT_LANGUAGE) -> str:
    """
    Resolve the Jellyfin user id for a language ('en' or 'fr'), matching it
    to the configured JELLYFIN_USER_EN / JELLYFIN_USER_FR account name.
    Cached per language for the lifetime of the process.
    """
    language = (language or _DEFAULT_LANGUAGE).lower()
    if language not in _LANGUAGE_USER_NAME:
        language = _DEFAULT_LANGUAGE
    if language in _user_id_by_language:
        return _user_id_by_language[language]

    target_name = _LANGUAGE_USER_NAME[language].strip().lower()
    users = await _get_all_users()
    match = next((u for u in users if u.get("Name", "").strip().lower() == target_name), None)
    if not match:
        # Fall back to the other configured account, then to whatever
        # exists, so the server still works if names don't match exactly.
        other = "fr" if language == "en" else "en"
        other_name = _LANGUAGE_USER_NAME[other].strip().lower()
        match = next(
            (u for u in users if u.get("Name", "").strip().lower() != other_name), None
        )
    if not match and users:
        match = users[0]
    if not match:
        raise RuntimeError("No users returned by Jellyfin /Users endpoint.")

    _user_id_by_language[language] = match["Id"]
    return match["Id"]


async def _resolve_person_id(person_name: str) -> Optional[str]:
    # /Persons is a library-wide endpoint, not scoped to a user's metadata
    # language — person (cast/crew) names aren't typically translated.
    data = await _get(
        "/Persons", {"SearchTerm": person_name, "Fields": "Name,Id", "EnableImages": "false"}
    )
    people = data.get("Items", [])
    return people[0]["Id"] if people else None


async def _resolve_series(series_name: str, language: str = _DEFAULT_LANGUAGE) -> Optional[dict[str, Any]]:
    """Find a series by name, matched against the given language's metadata."""
    user_id = await _resolve_user_id(language)
    data = await _get(
        f"/Users/{user_id}/Items",
        {
            "SearchTerm": series_name,
            "IncludeItemTypes": "Series",
            "Recursive": "true",
            "Fields": "Name,Id",
            "EnableImages": "false",
        },
    )
    items = data.get("Items", [])
    return items[0] if items else None


def _clean_for_tts(text: str) -> str:
    """Strip symbols that read awkwardly in text-to-speech output."""
    text = re.sub(r"[*/=#]", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse duplicate versions/copies of the same title, keep first seen."""
    seen: set[str] = set()
    result = []
    for item in items:
        name = item.get("Name")
        if name and name not in seen:
            seen.add(name)
            result.append(item)
    return result


def _format_items(items: list[dict[str, Any]], empty_message: str, with_year: bool = True) -> str:
    items = _dedupe(items)
    if not items:
        return empty_message
    lines = []
    for item in items:
        name = item["Name"]
        year = item.get("ProductionYear")
        lines.append(f"{name} ({year})" if with_year and year else name)
    return _clean_for_tts(", ".join(lines))


# --------------------------------------------------------------------------
# Core search — every free-form filter the old prompt could improvise lives
# here as an optional parameter, shared by every tool that needs to look
# items up, rather than as separate one-off tools.
# --------------------------------------------------------------------------


async def _find_items(
    item_type: str,
    search_term: str = "",
    genre: str = "",
    person_id: str = "",
    parent_id: str = "",
    year: Optional[int] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    watched: str = "any",  # any | watched | unwatched
    favorites_only: bool = False,
    min_rating: Optional[float] = None,
    studio: str = "",
    tag: str = "",
    official_rating: str = "",
    sort_by: str = "SortName",
    random_order: bool = False,
    limit: int = 10,
    fields: str = "Name,Id,ProductionYear",
    language: str = _DEFAULT_LANGUAGE,
) -> list[dict[str, Any]]:
    user_id = await _resolve_user_id(language)
    params: dict[str, Any] = {
        "IncludeItemTypes": item_type,
        "Recursive": "true",
        "Fields": fields,
        "EnableImages": "false",
        "Limit": limit,
    }
    if search_term:
        params["SearchTerm"] = search_term
    if genre:
        params["Genres"] = genre
    if person_id:
        params["PersonIds"] = person_id
    if parent_id:
        params["ParentId"] = parent_id
    if studio:
        params["Studios"] = studio
    if tag:
        params["Tags"] = tag
    if official_rating:
        params["OfficialRatings"] = official_rating
    if min_rating is not None:
        params["MinCommunityRating"] = min_rating

    if year is not None:
        params["Years"] = str(year)
    elif year_from is not None or year_to is not None:
        start = year_from or 1900
        end = year_to or datetime.utcnow().year
        params["MinPremiereDate"] = f"{start}-01-01T00:00:00.000Z"
        params["MaxPremiereDate"] = f"{end}-12-31T23:59:59.000Z"

    filters = []
    if watched == "watched":
        filters.append("IsPlayed")
    elif watched == "unwatched":
        filters.append("IsUnplayed")
    if favorites_only:
        filters.append("IsFavorite")
    if filters:
        params["Filters"] = ",".join(filters)

    if random_order:
        params["SortBy"] = "Random"
    else:
        params["SortBy"] = sort_by
        if sort_by in _DATE_SORT_FIELDS:
            params["SortOrder"] = "Descending"

    data = await _get(f"/Users/{user_id}/Items", params)
    return data.get("Items", [])


# --------------------------------------------------------------------------
# MCP server + tools
# --------------------------------------------------------------------------

mcp = MCPServer(
    name="jellyfin",
    instructions=(
        "Tools for searching, filtering, recommending, and remote-controlling "
        "a Jellyfin media server. Prefer the higher-level tools over raw "
        "endpoints; they already resolve user/person/series IDs and return "
        "concise, deduplicated, speech-friendly text. search_titles supports "
        "rich filtering (genre, year range, watched status, favorites, "
        "rating, studio, tag, parental rating) — use its parameters instead "
        "of trying multiple narrow searches.\n\n"
        "BILINGUAL LIBRARY: this household has two Jellyfin accounts against "
        "the same library — one with English metadata, one with French "
        "metadata (e.g. the same movie is 'Project Hail Mary' on one and "
        "'Projet dernière chance' on the other). Every data tool takes a "
        "`language` parameter ('en' or 'fr', default 'en'). Set it to match "
        "the language of: (1) the conversation, and (2) whichever language "
        "the title/genre/search term you're passing is written in — if the "
        "user gives you a French title or asks in French, use language='fr' "
        "so the search matches French metadata; mixing them (French search "
        "term against the English account, or vice versa) will fail to "
        "match. If the user is switching between languages mid-conversation, "
        "match language to whichever language their current message is in."
    ),
)


@mcp.tool(
    description=(
        "List Jellyfin user accounts (Name and Id only), and which language "
        "each configured account (JELLYFIN_USER_EN / JELLYFIN_USER_FR) "
        "resolves to. Rarely needed directly since other tools auto-resolve "
        "the right account from the `language` parameter, but useful for "
        "diagnostics or confirming the bilingual setup is configured correctly."
    )
)
async def list_users() -> dict[str, Any]:
    users = await _get_all_users()
    return {
        "users": [{"Name": u.get("Name", ""), "Id": u.get("Id", "")} for u in users],
        "language_mapping": _LANGUAGE_USER_NAME,
    }


@mcp.tool(
    description=(
        "Search/filter the media library. item_type must be one of: Movie, "
        "Series, Episode. All filters are optional and combine together "
        "(e.g. genre='Romance' + year_from=1990 + year_to=1999 finds 90s "
        "romance titles). Filters available:\n"
        "- search_term: match by title text\n"
        "- genre: e.g. 'Romance', 'Comedy', 'Action'\n"
        "- year: a single release year\n"
        "- year_from / year_to: release year range (use one or both for "
        "'from the 90s', 'after 2015', etc.)\n"
        "- watched: 'watched', 'unwatched', or 'any' (default)\n"
        "- favorites_only: true to only include the user's favorites\n"
        "- min_rating: minimum community rating, 0-10\n"
        "- studio: production studio/network name\n"
        "- tag: a Jellyfin tag\n"
        "- official_rating: parental rating, e.g. 'PG-13', 'TV-MA'\n"
        "- sort_by: SortName (default), DateCreated (newest added first), "
        "PremiereDate (newest release first), CommunityRating (highest "
        "rated first), or Random (equivalent to random_order=true)\n"
        "- language: 'en' or 'fr' (default 'en'). This is a bilingual "
        "household with separate English- and French-metadata accounts on "
        "the same library — set language to match the language of "
        "search_term/genre and of the conversation, e.g. language='fr' for "
        "'Projet dernière chance' or a French-language request.\n"
        "Returns a short, deduplicated, speech-friendly list of titles."
    )
)
async def search_titles(
    item_type: str,
    search_term: str = "",
    genre: str = "",
    year: Optional[int] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    watched: str = "any",
    favorites_only: bool = False,
    min_rating: Optional[float] = None,
    studio: str = "",
    tag: str = "",
    official_rating: str = "",
    sort_by: str = "SortName",
    random_order: bool = False,
    limit: int = 10,
    language: str = _DEFAULT_LANGUAGE,
) -> str:
    items = await _find_items(
        item_type=item_type,
        search_term=search_term,
        genre=genre,
        year=year,
        year_from=year_from,
        year_to=year_to,
        watched=watched,
        favorites_only=favorites_only,
        min_rating=min_rating,
        studio=studio,
        tag=tag,
        official_rating=official_rating,
        sort_by=sort_by,
        random_order=random_order,
        limit=limit,
        language=language,
    )
    return _format_items(items, f"I couldn't find any {item_type.lower()}s matching that.")


@mcp.tool(
    description=(
        "Get the most recently added items in the library (what Jellyfin's "
        "own 'Recently Added' row shows) — this is the right tool for "
        "questions like 'what's the latest movie in my library' or 'what did "
        "I just add'. item_type must be one of: Movie, Series, Episode. "
        "language: 'en' or 'fr' (default 'en') — returns titles in that "
        "language's metadata. Returns titles ordered newest-added first, "
        "with release year."
    )
)
async def get_recently_added(
    item_type: str = "Movie", limit: int = 5, language: str = _DEFAULT_LANGUAGE
) -> str:
    user_id = await _resolve_user_id(language)
    items = await _get(
        f"/Users/{user_id}/Items/Latest",
        {
            "IncludeItemTypes": item_type,
            "Fields": "Name,Id,ProductionYear",
            "EnableImages": "false",
            "Limit": limit,
        },
    )
    return _format_items(items, f"I couldn't find any recently added {item_type.lower()}s.")


@mcp.tool(
    description=(
        "Find movies or shows featuring a specific person (actor, director, "
        "writer, etc). Looks up the person's Jellyfin id first, then searches "
        "items for that person. Accepts the same optional filters as "
        "search_titles (genre, year_from/year_to, watched, favorites_only, "
        "min_rating, etc.), plus language: 'en' or 'fr' (default 'en') — "
        "note person names themselves usually don't change between "
        "languages, but set language to match the conversation so returned "
        "titles come back in the right language. Returns titles with year."
    )
)
async def search_by_person(
    person_name: str,
    item_type: str = "Movie",
    genre: str = "",
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    watched: str = "any",
    limit: int = 10,
    language: str = _DEFAULT_LANGUAGE,
) -> str:
    person_id = await _resolve_person_id(person_name)
    if not person_id:
        return f"I couldn't find anyone named {person_name} in the library."

    items = await _find_items(
        item_type=item_type,
        person_id=person_id,
        genre=genre,
        year_from=year_from,
        year_to=year_to,
        watched=watched,
        limit=limit,
        language=language,
    )
    return _format_items(items, f"I couldn't find any {item_type.lower()}s with {person_name}.")


@mcp.tool(
    description=(
        "Count total episodes available for a TV series by name (sums all "
        "duplicate copies, matching Jellyfin's TotalRecordCount when present). "
        "Only call this when the user explicitly asks how many episodes exist. "
        "language: 'en' or 'fr' (default 'en') — match the language of "
        "series_name and of the conversation."
    )
)
async def count_episodes(series_name: str, language: str = _DEFAULT_LANGUAGE) -> str:
    series = await _resolve_series(series_name, language)
    if not series:
        return f"I couldn't find a series called {series_name}."

    ep_data = await _get(
        "/Items",
        {
            "ParentId": series["Id"],
            "IncludeItemTypes": "Episode",
            "Recursive": "true",
            "Limit": 1,
            "Fields": "Name,Id",
            "EnableImages": "false",
        },
    )
    total = ep_data.get("TotalRecordCount")
    if total is None:
        total = len(ep_data.get("Items", []))

    return _clean_for_tts(f"There are {total} episodes of {series['Name']}.")


@mcp.tool(
    description=(
        "List the episodes of a TV series by series name, optionally filtered "
        "by watched status. Use this before queue_items when you need to know "
        "which specific episodes exist (e.g. 'what episodes of Bluey do I "
        "have'). language: 'en' or 'fr' (default 'en') — match series_name "
        "and the conversation. Returns titles in the order Jellyfin returns "
        "them."
    )
)
async def get_episodes(
    series_name: str, watched: str = "any", limit: int = 20, language: str = _DEFAULT_LANGUAGE
) -> str:
    series = await _resolve_series(series_name, language)
    if not series:
        return f"I couldn't find a series called {series_name}."
    items = await _find_items(
        item_type="Episode",
        parent_id=series["Id"],
        watched=watched,
        sort_by="SortName",
        limit=limit,
        language=language,
    )
    return _format_items(items, f"I couldn't find any episodes of {series['Name']}.", with_year=False)


@mcp.tool(
    description=(
        "Get random recommendations from the library filtered by genre. "
        "Accepts the same optional filters as search_titles (year_from/"
        "year_to, watched, favorites_only, min_rating) for requests like "
        "'recommend a few unwatched 90s romance movies'. language: 'en' or "
        "'fr' (default 'en') — genre names and returned titles must match "
        "that language's metadata (e.g. genre='Comédie' with language='fr'). "
        "Returns a short deduplicated list of titles."
    )
)
async def recommend_by_genre(
    genre: str,
    item_type: str = "Movie",
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    watched: str = "any",
    favorites_only: bool = False,
    min_rating: Optional[float] = None,
    limit: int = 3,
    language: str = _DEFAULT_LANGUAGE,
) -> str:
    items = await _find_items(
        item_type=item_type,
        genre=genre,
        year_from=year_from,
        year_to=year_to,
        watched=watched,
        favorites_only=favorites_only,
        min_rating=min_rating,
        random_order=True,
        limit=limit,
        language=language,
    )
    return _format_items(items, f"I couldn't find any {genre} titles to recommend.")


@mcp.tool(
    description=(
        "List active Jellyfin playback sessions (devices/players currently "
        "connected), so the user can choose where to cast playback or which "
        "device's queue to add to. Call this before play_on_device or "
        "queue_items if you don't already have a session_id."
    )
)
async def list_sessions() -> list[dict[str, str]]:
    sessions = await _get("/Sessions")
    return [
        {
            "SessionId": s.get("Id", ""),
            "DeviceName": s.get("DeviceName", ""),
            "Client": s.get("Client", ""),
            "UserName": s.get("UserName", ""),
        }
        for s in sessions
    ]


@mcp.tool(
    description=(
        "Search for a title by name and immediately start playing it on a "
        "given session/device (replaces whatever is currently playing). Call "
        "list_sessions first to get session_id if the user has more than one "
        "device. For adding items to the queue instead of playing "
        "immediately, use queue_items. language: 'en' or 'fr' (default 'en') "
        "— match item_name and the conversation, since the same movie may "
        "have a different title per language."
    )
)
async def play_on_device(
    item_name: str, session_id: str, item_type: str = "Movie", language: str = _DEFAULT_LANGUAGE
) -> str:
    items = await _find_items(item_type=item_type, search_term=item_name, limit=1, language=language)
    if not items:
        return f"I couldn't find {item_name} to play."
    item = items[0]

    await _post(
        f"/Sessions/{session_id}/Playing",
        {"ItemIds": item["Id"], "PlayCommand": "PlayNow"},
    )
    return _clean_for_tts(f"Playing {item['Name']} now.")


@mcp.tool(
    description=(
        "Add one or more items to the end of a session's current playback "
        "queue (does NOT interrupt what's currently playing) — this is the "
        "right tool for requests like 'add 5 episodes of Bluey to the "
        "current playlist' or 'add two romantic movies from the 90s to the "
        "queue'. Call list_sessions first to get session_id.\n"
        "How to fill the arguments:\n"
        "- For a specific series ('N episodes of X'): set item_type='Episode' "
        "and series_name='X'.\n"
        "- For a criteria-based pick ('N romantic movies from the 90s'): set "
        "item_type='Movie' (or 'Series'), and use genre/year_from/year_to/"
        "watched/favorites_only/min_rating as needed — same filters as "
        "search_titles.\n"
        "- For specific named titles, call this once per title with "
        "search_term set and count=1, or search_titles first to confirm the "
        "exact name.\n"
        "count controls how many matching items get added (e.g. 5, 2). "
        "position='last' (default) appends to the end of the queue; "
        "position='next' inserts right after the currently playing item. "
        "language: 'en' or 'fr' (default 'en') — match series_name/"
        "search_term/genre and the conversation language, since titles and "
        "genre names differ between the two accounts."
    )
)
async def queue_items(
    session_id: str,
    item_type: str = "Movie",
    count: int = 1,
    search_term: str = "",
    series_name: str = "",
    genre: str = "",
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    watched: str = "any",
    favorites_only: bool = False,
    min_rating: Optional[float] = None,
    position: str = "last",
    language: str = _DEFAULT_LANGUAGE,
) -> str:
    parent_id = ""
    if series_name:
        series = await _resolve_series(series_name, language)
        if not series:
            return f"I couldn't find a series called {series_name}."
        parent_id = series["Id"]
        item_type = "Episode"

    items = await _find_items(
        item_type=item_type,
        search_term=search_term,
        genre=genre,
        parent_id=parent_id,
        year_from=year_from,
        year_to=year_to,
        watched=watched,
        favorites_only=favorites_only,
        min_rating=min_rating,
        random_order=bool(genre or year_from or year_to or favorites_only or min_rating)
        and not series_name,
        limit=count,
        language=language,
    )
    if not items:
        return "I couldn't find anything matching that to add to the queue."

    item_ids = ",".join(item["Id"] for item in items)
    play_command = "PlayNext" if position == "next" else "PlayLast"
    await _post(
        f"/Sessions/{session_id}/Playing",
        {"ItemIds": item_ids, "PlayCommand": play_command},
    )

    names = [item["Name"] for item in _dedupe(items)]
    where = "next in the queue" if position == "next" else "to the end of the queue"
    return _clean_for_tts(f"Added {', '.join(names)} {where}.")


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------

if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(
            transport=transport,  # "sse" or "streamable-http"
            host=os.environ.get("MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("MCP_PORT", "8000")),
        )
