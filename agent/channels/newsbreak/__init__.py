"""NewsBreak, the second channel and the first managed one beside Google (spec 2026-09-29-admiry-channels-design
D88-D90; plan 2026-09-29-agent-m1-newsbreak): `client.py` speaks NewsBreak's Business API, `build.py` turns a plan into
its bodies, `creative.py` picks the licensed picture, and `adapter.py` runs them behind the channel protocol -- an ad
account per business, everything created paused and then turned on (M1 Task 5). Nothing is imported here:
`agent.channels` imports the adapter."""
