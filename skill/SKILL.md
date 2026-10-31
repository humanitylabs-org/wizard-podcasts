---
name: wizard-podcasts
description: Use when the owner asks about podcasts - following a show, new episodes, what he was listening to, or episode notes.
version: 0.3.2
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
- `podcasts_episode` returns show notes (`notes_text`), saved `position` in seconds, `duration`, `download.state`, and `file` (path relative to the Podcasts app data folder once downloaded). `transcript` is `not_available` in v1; `feed_transcript_url` is the publisher's own transcript link when the feed has one.
- `podcasts_mark` with `played: true|false` and/or `skipped: true|false` (optional `in_inbox`). Skipped = the owner dismissed it from his feed without listening (not played; its unstarted download is removed); `skipped: false` brings it back. Skipped episodes are left out of `view: "inbox"` and carry `skipped: true` in show episode lists. Read the returned episode to confirm. Only skip on the owner's word; he decides what to dismiss.

Playback happens in the Podcasts screen (`?episode=<id>` opens one episode). You cannot start audio on the owner's device. Downloads and new-episode checks run in the background on their own. If a tool returns an error, report it as written; never claim a show was followed or an episode marked without the returned readback.
