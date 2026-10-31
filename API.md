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
| GET | `/api/feed?show_id=&state=&sort=&q=&limit=&offset=` | The screen's list (0.3): `{episodes: [episode], total, offset, filter: {show_id, state, sort, q}, shows, now_playing}`. `state` `unplayed` (default: new and started, not finished, not skipped) \|`started`\|`played`\|`skipped` (dismissed without playing) \|`all` (every episode, 0.4.1); `q` (0.4.1, ≤ 200 characters) searches every episode of the followed shows that the other filters allow, server-side and case-insensitive: each word must appear in the episode title, the show name or the show notes (`total` counts every match; page with `offset`); `filter.q` echoes the normalised words (empty = no search); `sort` `newest` (default) \|`oldest` by `published`; `limit` default 100, max 500. `shows` lists every followed show, newest episode first: `{id, title, short_title, author, feed_url, artwork, auto_download, source, latest, unplayed, taste}` (`unplayed` = rows waiting in the feed; `taste` = his thumb on the show, 1/-1/0; the screen's Filters shows list draws from it). A bad value is a 400. |
| GET | `/api/inbox?show_id=&kind=&state=&sort=&since=&q=&limit=` | Up next (skipped episodes left out): `{continue: [episode], new: [episode], now_playing, filter, filters}`. All query values are optional and combine: `show_id` (one show), `kind` `podcast`\|`youtube` (`rss` = `podcast`; for agents, the screen does not offer it), `state` `started` (Continue only) \|`unplayed` (New only), `sort` `newest` (default) \|`oldest` orders `new` by `published` (`continue` stays most recently listened first), `since` (unix seconds) keeps only episodes published at or after it in both lists, `limit` caps `new` after sorting (default 100, max 500). Every episode carries `published` (unix seconds, UTC); the screen groups by it in the viewer's local time. `filter` echoes what was applied: `{show_id, kind, state, sort, since}`. `filters` always describes the whole, unfiltered Up next: `{shows: [{id, title, short_title, kind, artwork, unplayed, started, latest}] (newest episode first), kinds: {podcast, youtube}, started, unplayed, total}`. A bad value is a 400. `q` (0.4.1) narrows both lists to episodes whose title, show name or notes contain every word. The MCP tool `podcasts_list` takes the same `show_id`/`kind`/`state`/`sort`/`since`/`q` with `view: "inbox"`; with `q` and no `view` (or `view: "feed"`) it returns `/api/feed` (`show_id`, `state` incl. `all`, `sort`, `q`, `limit`, `offset`). |
| GET | `/api/episodes/{id}` | full episode (see fields below) |
| GET/HEAD | `/api/episodes/{id}/audio` | audio with **HTTP Range** (206 + `Content-Range`); local file when downloaded, otherwise the public enclosure streamed through the guarded fetcher |
| GET | `/api/settings` | `{storage_cap_mb, refresh_minutes, auto_download, speed, skip_back, skip_forward}` |
| GET | `/api/opml` | OPML export of followed shows |
| GET | `/api/discover?limit=&cursor=` | Discover (0.5.0): shows he might like, best first. `{items: [{feed_url, title, author, artwork, source: "rss"\|"youtube", genres, reason, kind: "for_you"\|"popular", score, taste, latest: {title, published, approx, playable}}], next_cursor, total, building, built_at}`. About 7 in 10 are `for_you` (similar to shows he follows or thumbed up; `reason` `Like <show>`, `Guest on <show>`, `More from <author>`) and 3 in 10 `popular` (`Popular in <genre>` / `Popular in the US`, `Popular in Thailand`). Never a followed show (same feed, Apple id, YouTube channel or name) or one he thumbed down; one card per show (a channel with a podcast of the same name folds into the podcast). `limit` 1–50 (default 20); page with `cursor` = the previous `next_cursor` (`null` at the end). `building: true` while the first pool is still being gathered (cards fill in over a few minutes). `latest.approx` = the date came from YouTube's "3 days ago". |
| GET/HEAD | `/api/discover/audio?feed_url=` | preview: the newest episode of a Discover show, streamed with Range like `/api/episodes/{id}/audio` (YouTube through the YouTube Music app). Nothing is followed or saved. |
| GET | `/api/taste` | his show taste: `{liked: [{title, feed_url, weight, why}], avoided_shows: [{title, feed_url, why}], liked_genres, avoided_genres, thumbs: {up, down}}` |

Episode fields: `id, show_id, show_title, title, published (unix), duration (s), position (s), position_at, played, in_inbox, skipped, download {state: none|queued|downloading|done|failed, progress_bytes, size_bytes, error}, file (relative to app data, e.g. episodes/3/41.mp3), audio, artwork`; detail adds `enclosure_url, mime, link, notes_html (sanitized), notes_text, transcript`.

`transcript` (v2 hook): `{status: "not_available", text: null, source: null, feed_transcript_url, planned}`. A v2 build fills `status`/`text`/`source` from the Transcriber app without changing other fields.

## Write

| Method | Path | Body | Effect |
|---|---|---|---|
| POST | `/api/subscribe` | `{url, auto_download?}` | follow a feed, a YouTube channel URL or an `@handle` (idempotent); newest episode to Up next and download queue. Shows carry `source: "rss"\|"youtube"`. |
| POST | `/api/shows/{id}` | `{auto_download}` | per-show auto-download |
| POST | `/api/shows/{id}/refresh` | `{}` | check that feed now |
| POST | `/api/shows/{id}/unsubscribe` | `{undo?: true}` | unfollow, delete its downloads. Remembered as a mild "less like this" for Discover unless `undo: true` (taking back a follow made a moment ago: Discover's Following toggle). Following the show again forgets it. |
| POST | `/api/shows/taste` | `{feed_url \| show_id, value: 1\|-1\|0}` | thumbs on a show (a Discover card or a followed show): `1` more like it (its similar shows rank higher), `-1` never recommend it (its genre and similar shows count less), `0` clear. Returns `{feed_url, value, taste}` (the `/api/taste` summary). |
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

## Discover: sources and taste (0.5.0)

Candidates come only from free public directories, cached 12 h (`discover_cache`) and rebuilt in the background at most twice a day (iTunes calls spaced 2.5 s apart; a source that failed, e.g. a chart feed timing out, is retried within the hour): Apple Podcasts top charts (`rss.applemarketingtools.com`, US + Thailand, `PODCASTS_DISCOVER_COUNTRIES`), Apple's top shows in the genres he likes (iTunes top-podcasts RSS by genre), Apple's "You Might Also Like" shelf on each liked show's public page, other shows by the same authors (iTunes Search), guests that recur across his shows' episode titles (their own show, iTunes Search), and channels YouTube pairs with his YouTube shows' newest uploads (the watch page's related column). Details and the newest episode come from the iTunes lookup API; a YouTube channel's newest upload from its public uploads feed or Videos tab. Only show names and ids are sent; his listening never leaves the box.

Taste lives in SQLite: `taste` (his thumbs only), `unfollows`, and the app's own episodes. Weights: follow +1, each episode heard through +0.25 (max +1.5), each skipped -0.05 (max -0.75), thumbs up +2.5, thumbs down -3 (and blocked), unfollowed -1. A card's score is the sum over the shows that point to it (Apple similar ×1, YouTube related ×0.8, guest ×0.6, same author ×0.5) plus half its genre affinity; a genre at or below -1 is avoided unless something he likes points there. One reason never fills the screen (each repeat counts 0.75^n).
