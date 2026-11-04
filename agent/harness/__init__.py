"""The home's thin harness (capability layer K6 slice 1, spec 2026-10-10-agent-k6-harness-consultation-design.md):
free text while a consultation is open or ready -- the code fast path (fast.py), else the material (context.py) and one
Turn call (turn.py), then code runs the one dispatch (dispatch.py). home_api.turn's `say` branch is its one caller."""
