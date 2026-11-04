"""Google Search, the first channel adapter (spec 2026-09-29-admiry-channels-design §5.1; plan
2026-09-29-agent-m0-channel-framework P1). `client`, `build` and `conversions` are A3 batch 1's `agent/google_ads.py`,
`agent/ads_launch.py` and `agent/ads_conversions.py`, moved here with their code unchanged (the old paths are import
shims for one release); `adapter` is the glue that makes them one adapter behind the protocol in `channels/base.py`.
Nothing is imported here: `agent.channels` imports the adapter, and the adapter imports what it needs."""
