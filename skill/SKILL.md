---
name: wizard-podcasts
description: Use when the owner asks about podcasts - following a show, new episodes, what he was listening to, or episode notes.
version: 0.5.1
author: Humanity Labs
license: GPL-3.0-only
platforms: [linux, macos]
metadata:
  hermes:
    tags: [podcasts, audio]
    related_skills: []
---
# Podcasts

The owner's private podcast player (app id `wizard-podcasts`). Use its MCP tools; never fetch feeds yourself.

- `podcasts_status` first: shows, new episodes, download queue/failures, storage used vs cap, what is playing and where.
- Follow a show: `podcasts_search` with its name, pick the matching result (title + author), then `podcasts_subscribe` with that result's `feed_url`. A pasted RSS/Atom address goes straight to `podcasts_subscribe`. Idempotent. Only public feed enclosures are used.
- YouTube-only shows: `podcasts_subscribe` also takes a YouTube channel URL (`https://www.youtube.com/@handle`) or `@handle`; `podcasts_search` returns matching channels under `youtube` (pick by name + handle + subscribers; prefer the creator's own channel over clips/fan channels). Episodes are long-form uploads, audio only, through the YouTube Music app; if `podcasts_status.youtube.installed` is false, tell the owner to get YouTube Music. Show rows have `source: "youtube"`.
- `podcasts_list` with `view: "inbox"` returns what is waiting (`continue` = started, `new` = unplayed new; the screen shows both as one feed in date order). Narrow it with `show_id`, `state` (`started`|`unplayed`), `since` (unix seconds: only episodes published at or after it; e.g. "what came out today") and, for agents only, `kind` (`podcast`|`youtube`; the screen treats both as one list). `sort` `newest` (default) or `oldest` orders `new` by publish date. Every episode has `published` (unix seconds, UTC): convert it to the owner's local time before saying "today"/"yesterday". Its `filters.shows` lists the shows in Up next newest first with `unplayed` counts. No arguments lists shows; `show_id` without `view` lists that show's episodes newest first.
- Find an episode: `podcasts_list` with `q` (words matched case-insensitively in episode titles, show names and show notes; every word must match). Without `view` it searches every episode of the followed shows that are still waiting (`state` `unplayed`); pass `state: "all"` for everything incl. played, or `played`|`started`|`skipped`; narrow with `show_id`, order with `sort`, page with `limit`/`offset` (`total` = all matches). With `view: "inbox"` it narrows Up next.
- `podcasts_episode` returns show notes (`notes_text`), saved `position` in seconds, `duration`, `download.state`, and `file` (path relative to the Podcasts app data folder once downloaded). `transcript` is `not_available` in v1; `feed_transcript_url` is the publisher's own transcript link when the feed has one.
- `podcasts_mark` with `played: true|false` and/or `skipped: true|false` (optional `in_inbox`). Skipped = the owner dismissed it from his feed without listening (not played; its unstarted download is removed); `skipped: false` brings it back. Skipped episodes are left out of `view: "inbox"` and carry `skipped: true` in show episode lists. Read the returned episode to confirm. Only skip on the owner's word; he decides what to dismiss.

- Recommendations: `podcasts_discover` returns shows he might like, best first (`reason` says why: `Like <show>`, `Guest on <show>`, `More from <author>`, `Popular in <genre or country>`), each with its newest episode; never a show he follows or thumbed down. Page with `cursor` = `next_cursor`. `building: true` means the first list is still being gathered; try again in a minute. To follow one, pass its `feed_url` to `podcasts_subscribe`.
- Taste: `podcasts_taste` with no `value` summarises what he likes and avoids (shows and genres). With `value` it sets his thumb on one show (`feed_url` from Discover or `show_id` of a followed show): 1 = more like it, -1 = never recommend it (also dulls its genre and similar shows), 0 = clear. Only on his word; report the returned summary.

Playback happens in the Podcasts screen (`?episode=<id>` opens one episode, `?show=<id>` one show, `?tab=discover` Discover). The screen has three buttons: My feed, Discover (recommended shows with thumbs up/down, Follow and a tap to preview the newest episode; plus search) and Filters (search episodes, show, state, order, and each followed show with Unfollow). You cannot start audio on the owner's device. Downloads and new-episode checks run in the background on their own. If a tool returns an error, report it as written; never claim a show was followed or an episode marked without the returned readback.
