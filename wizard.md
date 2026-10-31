---
wizard: 0.2
id: wizard-podcasts
name: Podcasts
type: service
category: app
version: 0.5.3
voice_names: [podcasts, podcast, podcast player]
summary: Your private podcast player. Follow shows, new episodes download on their own, and your place is saved across devices.
tags: [audio, learning]
license: GPL-3.0-only
source: https://github.com/humanitylabs-org/wizard-podcasts
icon: icon.png
runtime:
  kind: runtipi
  config: runtipi/config.json
  compose: runtipi/docker-compose.yml
api:
  port: 9207
  path: /api
  spec: API.md
mcp:
  path: /mcp
  port: 9207
  transport: streamable-http
skill: skill/SKILL.md
view:
  path: /
  port: 9207
  source:
    path: web
    build: python3 app/build_view.py
    dist: web
health: /healthz
provides: [podcasts]
uses: [music-source]
needs: []
---

# Podcasts

A private podcast player on your own Wizard. Search the free Apple Podcasts directory or paste any RSS/Atom feed (OPML import and export too). Feeds are checked in the background, new episodes land in your feed and download to the app's own storage under a size cap. Your place in every episode is saved on the Wizard, so phone and desktop pick up where the other stopped. Speed 0.8–3×, skip back 15 s / forward 30 s, mark played.

Podcast episodes always come from each show's public feed enclosure. YouTube channels can be followed as shows too: their long-form uploads arrive as audio only through the YouTube Music app (the `music-source` connector Podcasts uses), with the same feed, downloads and saved place. Feed, search, artwork and audio requests refuse private, loopback and tailnet addresses; downloads are size-capped; show notes are sanitized.

Discover is a feed of shows you might like, learned privately on your Wizard from what you follow, thumb up or down, listen through and skip (similar shows from Apple Podcasts and YouTube, guests your shows share, and what is popular), with a one-tap preview of each show's newest episode.

Agent tools (MCP): `podcasts_status`, `podcasts_search`, `podcasts_subscribe`, `podcasts_list`, `podcasts_episode`, `podcasts_mark`, `podcasts_discover`, `podcasts_taste`. Transcripts are planned for v2 through the Transcriber app (see README).
