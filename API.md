# Podcasts API

Base: the app root (`/` on port 9207, or `/wizard-podcasts/` through the Wizard gateway; both work). JSON in and out. Writes need `Content-Type: application/json` and a same-origin or trusted `Origin`. The gateway owns authentication; the direct port is loopback only.

## Read

| Method | Path | Returns |
|---|---|---|
| GET | `/healthz` | `{ok, app, version}` |
| GET | `/api/status` | counts, download queue/failures, `storage_used_mb`, `storage_cap_mb`, `now_playing`, settings, `youtube: {shows, installed, ready, playback, message}` (the YouTube Music connector) |
| GET | `/api/search?q=&limit=` | `results`: Apple Podcasts directory `{title, author, feed_url, artwork, genre, subscribed, source:"rss"}`; `youtube`: channels `{title, author (handle), feed_url (channel URL), artwork, subscribers, channel_id, subscribed, source:"youtube"}`. A channel URL or `@handle` as `q` returns that one channel. |
| GET | `/api/shows` | `{shows: [show]}` with `episodes`, `unplayed`, `new`, `downloaded` counts |
| GET | `/api/shows/{id}?limit=&offset=` | `{show, episodes: [episode]}` newest first |
| GET | `/api/shows/{id}/art` | cached artwork image |
| GET | `/api/inbox` | Up next: `{continue: [episode], new: [episode], now_playing}` |
| GET | `/api/episodes/{id}` | full episode (see fields below) |
| GET/HEAD | `/api/episodes/{id}/audio` | audio with **HTTP Range** (206 + `Content-Range`); local file when downloaded, otherwise the public enclosure streamed through the guarded fetcher |
| GET | `/api/settings` | `{storage_cap_mb, refresh_minutes, auto_download, speed, skip_back, skip_forward}` |
| GET | `/api/opml` | OPML export of followed shows |

Episode fields: `id, show_id, show_title, title, published (unix), duration (s), position (s), position_at, played, in_inbox, download {state: none|queued|downloading|done|failed, progress_bytes, size_bytes, error}, file (relative to app data, e.g. episodes/3/41.mp3), audio, artwork`; detail adds `enclosure_url, mime, link, notes_html (sanitized), notes_text, transcript`.

`transcript` (v2 hook): `{status: "not_available", text: null, source: null, feed_transcript_url, planned}`. A v2 build fills `status`/`text`/`source` from the Transcriber app without changing other fields.

## Write

| Method | Path | Body | Effect |
|---|---|---|---|
| POST | `/api/subscribe` | `{url, auto_download?}` | follow a feed, a YouTube channel URL or an `@handle` (idempotent); newest episode to Up next and download queue. Shows carry `source: "rss"\|"youtube"`. |
| POST | `/api/shows/{id}` | `{auto_download}` | per-show auto-download |
| POST | `/api/shows/{id}/refresh` | `{}` | check that feed now |
| POST | `/api/shows/{id}/unsubscribe` | `{}` | unfollow, delete its downloads |
| POST | `/api/refresh` | `{}` | check every feed now |
| POST | `/api/episodes/{id}/progress` | `{position, duration?, completed?}` | save place (shared across devices); `completed: true` marks played |
| POST | `/api/episodes/{id}/mark` | `{played, in_inbox?}` | played (leaves Up next) / unplayed |
| POST | `/api/episodes/{id}/download` | `{}` | queue a download |
| POST | `/api/episodes/{id}/delete-download` | `{}` | stop or remove a download |
| POST | `/api/settings` | partial settings | `storage_cap_mb` 256–512000, `refresh_minutes` 15–1440, `speed` 0.8–3, `auto_download` |
| POST | `/api/opml` | `{opml: "<xml>"}` (≤ 2 MB) | import; shows load in the background |
| POST | `/mcp` | JSON-RPC 2.0 | MCP tools (box-only: refused when forwarded/tailnet headers are present) |

Errors: `{error}` with 400/404/413/415/422/502.
