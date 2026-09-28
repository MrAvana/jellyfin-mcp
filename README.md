# Jellyfin MCP Server

Converts your n8n "AI agent + raw HTTP request tool + giant instruction
prompt" setup into a real MCP server. Instead of an LLM assembling
Jellyfin URLs from a system prompt, each capability is now a typed tool:

| Tool | Replaces from the original prompt |
|---|---|
| `list_users` | manual `/Users` lookup step |
| `search_titles` | general item search (movies/series/episodes), with rich optional filters — see below |
| `get_recently_added` | "what's the latest movie in my library" — wraps Jellyfin's own `/Items/Latest` (Recently Added) endpoint |
| `search_by_person` | `/Persons` lookup + `personIds` item search, combinable with the same filters as `search_titles` |
| `count_episodes` | series search + episode count + sum duplicates |
| `get_episodes` | list a series' episodes, optionally by watched status |
| `recommend_by_genre` | genre-filtered random recommendations, combinable with the same filters |
| `list_sessions` | "Jellyfin Get Players" step |
| `play_on_device` | search + `POST /Sessions/{id}/Playing` with `PlayNow` (interrupts current playback) |
| `queue_items` | search (by name, series, or criteria) + `POST /Sessions/{id}/Playing` with `PlayNext`/`PlayLast` (adds to queue) |

### Filters available on `search_titles` / `search_by_person` / `recommend_by_genre` / `queue_items`

All the free-form filtering the original raw-HTTP agent could improvise is now available as optional parameters, shared across these tools via one internal `_find_items()` helper (see "Design choices" below for why it's parameters on a few tools rather than a tool per filter):

- `genre` — e.g. `Romance`, `Comedy`
- `year` — a single release year
- `year_from` / `year_to` — release year range (e.g. "from the 90s" → `year_from=1990, year_to=1999`)
- `watched` — `watched`, `unwatched`, or `any`
- `favorites_only` — restrict to the user's favorites
- `min_rating` — minimum community rating (0–10)
- `studio` — production studio/network
- `tag` — a Jellyfin tag
- `official_rating` — parental rating, e.g. `PG-13`, `TV-MA`
- `sort_by` — `SortName`, `DateCreated`, `PremiereDate`, `CommunityRating`, or `Random`

### Queueing ("add N episodes/movies to the current playlist")

`queue_items` is what "add 5 episodes of Bluey to the current playlist" and "add two romantic movies from the 90s to the current playlist" map to. A couple of things worth knowing:

- **"Current playlist" = the active session's playback queue**, not a saved Jellyfin Playlist library item. It calls Jellyfin's session-queue endpoint (`POST /Sessions/{id}/Playing` with `PlayCommand=PlayNext` or `PlayLast`), the same mechanism a Jellyfin client uses when you hit "Play Next"/"Add to Queue" in the UI. It does **not** interrupt whatever's currently playing.
- If instead you want a persistent, named Jellyfin Playlist (visible in the library, survives across sessions), that's a different API (`POST /Playlists`) and isn't implemented yet — say the word if you want that added as a separate tool; it's a genuinely different concept from the playback queue, so it wouldn't reuse `queue_items`.
- The agent needs a `session_id` first — make sure your system prompt tells it to call `list_sessions` when the user hasn't specified a device (or ask, if there are multiple).

User-ID resolution, person-ID resolution, deduplicating multiple versions
of the same title, and stripping symbols for text-to-speech are now all
handled in code, so your n8n agent's system prompt can shrink to just a
few lines of *behavioral* guidance (tone, when to ask which device to
play on, etc.) instead of API mechanics. `DELETE` requests are not
implemented anywhere in this server, so there's no way for a model to
issue one.

### Bilingual libraries (two accounts, same library, different metadata language)

If your household has two Jellyfin accounts against the same library —
one with English metadata, one with French (so the same movie shows up
as "Project Hail Mary" on one and "Projet dernière chance" on the
other) — every data tool takes a `language` parameter (`"en"` or
`"fr"`, default `"en"`) that picks which account to query. It's
resolved by matching against `JELLYFIN_USER_EN` / `JELLYFIN_USER_FR`
(see Configure below) and cached per language, so it costs one extra
`/Users` lookup total, not one per call.

The model needs to actually set `language` correctly — the tool
descriptions tell it to match the language of the conversation and of
whatever title/genre it's passing in, but if your agent still defaults
to English, add an explicit line to the AI Agent's system prompt, e.g.:

> This household has two Jellyfin metadata languages, English and
> French. Always pass `language='fr'` when the user is speaking French
> or gives you a French title, and `language='en'` otherwise.

Two things this does *not* do, in case you need them later:
- It doesn't translate on the fly — "Projet dernière chance" only
  matches because the French account's own metadata is already in
  French (Jellyfin fetched French metadata for that account). If a
  title has no French metadata, searching it in French will find nothing.
- It doesn't mix languages within one call — a genre value like
  `'Comédie'` must be searched with `language='fr'`, not `'en'`,
  since genre names are also per-account metadata.

## 1. Configure

Set these environment variables (e.g. in a `.env` file, your process
manager, or the Docker `-e` flags):

```
JELLYFIN_URL=http://127.0.0.1:8096
JELLYFIN_API_KEY=your-jellyfin-api-key      # Jellyfin Dashboard -> API Keys
JELLYFIN_USER_EN=Bob                       # account name with English metadata
JELLYFIN_USER_FR=Bob French                # account name with French metadata
```

**Never commit the actual API key to Git!** Use your system's
environment management tools (like `.env`, Docker secrets, or cloud
provider secret stores) instead.

## 2. Run

### Locally (stdio) — for Claude Desktop or local MCP clients

```bash
pip install -r requirements.txt
python server.py
```

### As an HTTP server — for n8n's MCP Client Tool node

```bash
pip install -r requirements.txt
MCP_TRANSPORT=streamable-http MCP_HOST=0.0.0.0 MCP_PORT=8000 python server.py
```

The MCP endpoint will be available at `http://<host>:8000/mcp`.

### With Docker

```bash
docker build -t jellyfin-mcp .
docker run -d --name jellyfin-mcp \
  --network host \
  -e JELLYFIN_URL=http://127.0.0.1:8096 \
  -e JELLYFIN_API_KEY=your-jellyfin-api-key \
  jellyfin-mcp
```

(`--network host` is the easiest way to reach a Jellyfin instance on
`127.0.0.1`; swap for a shared Docker network / service name if Jellyfin
runs in its own container.)

## 3. Wire it into n8n

n8n's **MCP Client Tool** node can call this server directly — you no
longer need the AI Agent node's system prompt full of URL templates:

1. Add an **MCP Client Tool** node (or, in an AI Agent's Tools list,
   choose "MCP Client Tool").
2. **Connection type**: `HTTP Streamable` (or `SSE` if you ran with
   `MCP_TRANSPORT=sse`).
3. **HTTP Streamable URL**: `http://<host>:8000/mcp`
4. Leave "Tools to Include" as **All**, or select just the ones you want
   exposed to that agent.
5. In your AI Agent node's system prompt, you can now trim it down to
   behavior only, e.g.:

   > You control a Jellyfin media server via the connected tools. Keep
   > answers short and speech-friendly. If several devices are available,
   > ask which one before calling play_on_device.

The agent will call `search_titles`, `search_by_person`,
`recommend_by_genre`, `count_episodes`, `list_sessions`, and
`play_on_device` as native tool calls — no more manual URL construction,
no more `Field_Containing_Data` bookkeeping.

## Notes / design choices

- **No DELETE support**: mirrors the original "Never use DELETE" rule —
  it's structurally impossible, not just a prompt instruction.
- **User ID caching**: resolved once per process instead of once per
  request, cutting the round-trips the original agent made on every turn.
- **TTS-friendly output**: search/recommend/queue tools dedupe multiple
  copies of the same title and strip `* / = #` from the response text,
  same as the original prompt's formatting rule — but guaranteed in code
  rather than hoped-for from an LLM.
- **Filters as parameters, not tools**: every tool definition (name +
  description + JSON schema) is resent to the model as context on every
  single agent turn in n8n — that's the actual token cost, not how many
  query patterns are supported. A dozen narrow tools (`search_by_year`,
  `search_unwatched`, `search_favorites`...) would grow that cost on
  every turn; the same filters as optional parameters on `search_titles`
  add schema size once, not per filter. Current 10-tool schema set is
  roughly 2,300–2,400 tokens total (measured directly from the tool
  definitions) — noticeable, but not the "spike per request" that one
  tool per filter would cause as more filters get added over time. If it
  ever becomes a real bottleneck, two options that don't require
  redesigning the tools: (1) use the MCP Client Tool node's "Tools to
  Include" to only expose the subset a given agent actually needs, or
  (2) check whether your model provider's API supports prompt caching
  for tool definitions (Claude's does) and whether n8n's node takes
  advantage of it — that would make the repeated-per-turn cost mostly
  free after the first call in a session.
- **Bilingual resolution is per-language, not per-request**: the two
  account IDs are resolved once (on first use of each language) and
  cached, same principle as the original single-user caching — adding a
  second language doesn't add a round trip per tool call.
- Extend by adding more `@mcp.tool()` functions in `server.py` (e.g.
  persistent named playlists via `/Playlists`, "mark as watched")
  following the same pattern.
