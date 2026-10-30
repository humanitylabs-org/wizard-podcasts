# Podcasts

A private podcast player for [Wizard OS](https://www.aiwizards.com/wizard-apps). Install it from the Wizard App Store (`wam install wizard-podcasts`).

- **Follow shows**: search the free Apple Podcasts directory (iTunes Search API), paste any RSS/Atom feed, or import OPML. Export your list as OPML any time.
- **YouTube channels as shows**: paste a channel URL (`youtube.com/@handle`, `/channel/UC…`) or `@handle`, or pick a channel from search. Episodes are the channel's long-form uploads from YouTube's public feed (`feeds/videos.xml?playlist_id=UULF…`, so no Shorts or live streams), and arrive as **audio only** through the installed **YouTube Music** app (`wizard-youtube-music`, contract `wizard.music-source/1`, `/api/music/audio/<videoId>`, including its home relay). Same feed, download queue, storage cap, saved place and mark played as podcasts; the feed treats podcasts and YouTube as one list. Without YouTube Music the shows still update and say which app to get.
- **One screen**: the feed of episodes from the shows you follow, newest first, under quiet date labels in your local time (Today, Yesterday, this week's weekdays, Last week, then months). Started episodes stay in place with a thin progress line; played ones are hidden. Each row is artwork, title, show, when it was posted and how long it runs. On top: one row of show chips (All, then every followed show, newest episode first) plus a filter button (Unplayed / Started / Played, Newest / Oldest first, remembered per device) and a + button to find shows. Nested screens always show Back. Long-press (or right-click) a chip, or tap the chosen chip again, for that show's actions: all episodes, auto-download, unfollow. The find screen searches podcasts and YouTube channels in one list, opens Episodes for shows already followed, follows and opens a new show in one tap, takes a pasted feed address or `@handle`, and holds settings (storage, feed checks, OPML import/export). A compact player bar sits at the bottom while something is loaded; tap it for the full player.
- **Auto-download** new episodes into the app's own storage, with a storage cap (default 2 GB). When full, played downloads go first, then the oldest unstarted ones. Per-episode limit 1 GB.
- **Play** in the app: speed 0.8–3×, back 15 s / forward 30 s, seeking (HTTP Range), lock-screen controls where the browser supports them. Episodes that are not downloaded yet stream from the publisher through the app.
- **Your place is saved on the Wizard**, so the phone and desktop resume from the same spot. Mark played/unplayed, with Undo.
- **Agent tools (MCP)**: `podcasts_status`, `podcasts_search`, `podcasts_subscribe`, `podcasts_list`, `podcasts_episode`, `podcasts_mark`. See `skill/SKILL.md` and `API.md`.

## Safety

- Podcast audio is only the public feed enclosure. YouTube audio comes only from the YouTube Music connector; Podcasts never contacts googlevideo itself. YouTube lookups (channel page, uploads feed, channel search, avatars) go only to youtube.com, ytimg.com, ggpht.com and yt3.googleusercontent.com, checked on every redirect hop on top of the public-address guard.
- Every outbound request (feeds, search, artwork, audio) accepts only `http`/`https`, resolves the host once, refuses loopback, private, link-local, CGNAT/Tailscale, multicast and reserved addresses, connects to that checked address, and re-checks every redirect.
- Feeds ≤ 64 MB (`PODCASTS_MAX_FEED_MB`; parsed as a stream, so memory stays small), redirect chains ≤ 15 hops (each hop re-checked), artwork ≤ 5 MB, OPML ≤ 2 MB, episodes ≤ 1 GB (`PODCASTS_MAX_EPISODE_MB`), and a total storage cap. XML with DTDs/entities is refused.
- Show notes are sanitized on the server (allow-list of text tags and http(s)/mailto links; no scripts, styles, frames, images or attributes) and rebuilt node by node in the browser. The page's CSP allows only its own scripts and styles.
- MCP tools are box-only (refused when tailnet/forwarded headers are present); the gateway owns authentication.

## Transcripts (v2, not built)

TODO(v2): transcribe downloaded episodes with the installed **Transcriber** app (Wizard's shared speech-to-text service, `http://transcriber:8000/v1`, OpenAI-compatible `/audio/transcriptions`). The hook is already in place:

- Every episode detail (`GET /api/episodes/{id}`, MCP `podcasts_episode`) has a `transcript` object: `{status: "not_available", text: null, source: null, feed_transcript_url, planned}`. v2 fills `status` (`queued|running|done|failed`), `text` and `source` (`transcriber` or `feed`) without changing any other field.
- `feed_transcript_url` is already parsed from `<podcast:transcript>` (Podcasting 2.0) when the publisher provides one.
- Input file: `file` (relative to the app data folder) once `download.state` is `done`.

## Develop

```sh
DATA_DIR=.data PORT=9207 BIND=127.0.0.1 python3 app/server.py   # stdlib only
python3 -m pytest -q tests
wam check .                                                     # Wizard builder checks
```

Layout: `app/server.py` (HTTP + MCP), `app/podcasts.py` (service layer, downloads, refresh), `app/feeds.py` (RSS/Atom/OPML parsing, sanitizer), `app/netfetch.py` (SSRF-guarded fetcher), `app/youtube.py` (channel resolve/search, audio through the music-source connector), `web/` (view: vanilla JS on the shared Wizard kit and `WizardApp` client), `runtipi/` (store recipe). The image is built for amd64 and arm64 by the shared Wizard app-image workflow and pinned by digest in `runtipi/docker-compose.yml`.

License: GPL-3.0-only (adapts the Wizard App Manager hello-wizard template; see NOTICE.md).
