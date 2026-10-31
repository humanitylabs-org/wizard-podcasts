# Podcasts API

Base: the app root (`/` on port 9207, or `/wizard-podcasts/` through the Wizard gateway; both work). JSON in and out. Writes need `Content-Type: application/json` and a same-origin or trusted `Origin`. The gateway owns authentication; the direct port is loopback only.

## Read

| Method | Path | Returns |
|---|---|---|
| GET | `/healthz` | `{ok, app, version}` |
| GET | `/api/status` | counts, download queue/failures, `storage_used_mb`, `storage_cap_mb`, `now_playing`, settings, `youtube: {shows, installed, ready, playback, message}` (the YouTube Music connector) |
| GET | `/api/search?q=&limit=` | `results`: Apple Podcasts directory `{title, author, feed_url, artwork, genre, subscribed, show_id, source:"rss"}`; `youtube`: channels `{title, author (handle), feed_url (channel URL), artwork, subscribers, channel_id, subscribed, show_id, source:"youtube"}`. A channel URL or `@handle` as `q` returns that one channel. |
| GET | `/api/shows` | `{shows: [show]}` with `episodes`, `unplayed`, `new`, `downloaded` counts |
| GET | `/api/shows/{id}?limit=&offset=` | `{show, episodes: [episode]}` newest first |
| GET | `/api/shows/{id}/art` | cached artwork image |
| GET | `/api/feed?show_id=&state=&sort=&limit=&offset=` | The screen's list (0.3): `{episodes: [episode], total, offset, filter: {show_id, state, sort}, shows, now_playing}`. `state` `unplayed` (default: new and started, not finished, not skipped) \|`started`\|`played`\|`skipped` (dismissed without playing); `sort` `newest` (default) \|`oldest` by `published`; `limit` default 100, max 500. `shows` lists every followed show, newest episode first: `{id, title, short_title, author, feed_url, artwork, auto_download, source, latest, unplayed}` (`unplayed` = rows waiting in the feed; the screen's Filters show picker and Discover's followed-shows list draw from it). A bad value is a 400. |
| GET | `/api/inbox?show_id=&kind=&state=&sort=&since=&limit=` | Up next (skipped episodes left out): `{continue: [episode], new: [episode], now_playing, filter, filters}`. All query values are optional and combine: `show_id` (one show), `kind` `podcast`\|`youtube` (`rss` = `podcast`; for agents, the screen does not offer it), `state` `started` (Continue only) \|`unplayed` (New only), `sort` `newest` (default) \|`oldest` orders `new` by `published` (`continue` stays most recently listened first), `since` (unix seconds) keeps only episodes published at or after it in both lists, `limit` caps `new` after sorting (default 100, max 500). Every episode carries `published` (unix seconds, UTC); the screen groups by it in the viewer's local time. `filter` echoes what was applied: `{show_id, kind, state, sort, since}`. `filters` always describes the whole, unfiltered Up next: `{shows: [{id, title, short_title, kind, artwork, unplayed, started, latest}] (newest episode first), kinds: {podcast, youtube}, started, unplayed, total}`. A bad value is a 400. The MCP tool `podcasts_list` takes the same `show_id`/`kind`/`state`/`sort`/`since` with `view: "inbox"`. |
| GET | `/api/episodes/{id}` | full episode (see fields below) |
| GET/HEAD | `/api/episodes/{id}/audio` | audio with **HTTP Range** (206 + `Content-Range`); local file when downloaded, otherwise the public enclosure streamed through the guarded fetcher |
| GET | `/api/settings` | `{storage_cap_mb, refresh_minutes, auto_download, speed, skip_back, skip_forward}` |
| GET | `/api/opml` | OPML export of followed shows |

Episode fields: `id, show_id, show_title, title, published (unix), duration (s), position (s), position_at, played, in_inbox, skipped, download {state: none|queued|downloading|done|failed, progress_bytes, size_bytes, error}, file (relative to app data, e.g. episodes/3/41.mp3), audio, artwork`; detail adds `enclosure_url, mime, link, notes_html (sanitized), notes_text, transcript`.

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
| POST | `/api/episodes/{id}/mark` | `{played?, skipped?, in_inbox?}` | played (leaves Up next) / unplayed; `skipped` as below. At least one of `played`/`skipped`. |
| POST | `/api/episodes/{id}/skip` | `{skipped: true\|false}` (default true) | Skip (0.3.1): hide from the feed and Up next without marking played, like dismissing a notification. A queued or running download is cancelled at once; a finished download of an unstarted episode is deleted after a short Undo window (180 s, `PODCASTS_SKIP_GRACE_SECONDS`) to free space; a started episode keeps its file. `skipped: false` brings it back and re-queues a download the skip removed. Saving a position > 0 (listening) or marking played clears the skip. Returns the episode. |
| POST | `/api/shows/{id}/skip-all` | `{skipped?: true\|false, episode_ids?: [id]}` | Skip every episode of the show waiting in the feed: `{show_id, skipped, count, episode_ids}`. Undo with `{skipped: false, episode_ids}` from that answer (without `episode_ids`, every skipped episode of the show comes back). New episodes keep arriving. |
| POST | `/api/episodes/{id}/download` | `{}` | queue a download |
| POST | `/api/episodes/{id}/delete-download` | `{}` | stop or remove a download |
| POST | `/api/settings` | partial settings | `storage_cap_mb` 256–512000, `refresh_minutes` 15–1440, `speed` 0.8–3, `auto_download` |
| POST | `/api/opml` | `{opml: "<xml>"}` (≤ 2 MB) | import; shows load in the background |
| POST | `/mcp` | JSON-RPC 2.0 | MCP tools (box-only: refused when forwarded/tailnet headers are present) |

Errors: `{error}` with 400/404/413/415/422/502.
