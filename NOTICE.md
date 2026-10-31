# Source and license notice

Podcasts adapts the Wizard App Manager `hello-wizard` reference template (GPL-3.0-only): its stdlib HTTP/MCP server pattern, build check and the pinned Wizard OS appearance snapshot in `web/` (`wizard-tokens.css`, `wizard-ui.css`, `wizard-sheet.js`, `boot.js`, recorded in `appearance-provenance.json`, unmodified). The podcast service, feed parser, fetcher, view and packaging are new (Humanity Labs, October 2026). Distributed under GPL-3.0-only; no warranty.

The view loads the centrally maintained `/wizard-os-home/core/app-client.js` (shared WizardApp functions); no platform code is copied into this app. Podcast search uses Apple's public iTunes Search API; episode audio comes from each publisher's public feed enclosure. Python and the base image keep their upstream licenses.

The settings gear icon path is from Feather Icons (MIT, Cole Bemis).

The thumbs up/down icon paths are from Lucide (ISC License, Lucide Contributors). Discover reads public directory data from Apple (Apple Podcasts charts, iTunes Search/lookup, public show pages) and YouTube (public channel and watch pages); no account or key is used.
