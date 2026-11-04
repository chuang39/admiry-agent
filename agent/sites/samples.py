"""The two sample sites the look is reviewed on (A3p-6 batch 1 Q10): fictional businesses whose words are the design
canvas's. A sample's `words` are its family's base artboard's (Solid, Calm); its `style_words` hold, per other style,
only what that style's artboard words differently (Q4 still decides the home headline), laid over the base by `load`."""

from __future__ import annotations

import json
from pathlib import Path

from . import spec

DIR = Path(__file__).resolve().parent.parent / "data" / "sites" / "samples"


def names() -> list[str]:
    """The roofer and the dentist (the design canvas's), then a sample for each other home-services trade (the founder's
    dev walk, 2026-10-08: an AC contractor picked its look from a roofer's site), whose words the model wrote from the
    sample's facts and the linter passed."""
    return ["roofer", "dentist", "hvac", "plumber", "electrician"]


def _over(base: dict, over: dict) -> dict:
    """`base` with `over` laid on it: a dict merges key by key, anything else (a line, a list) replaces."""
    out = dict(base)
    for key, value in over.items():
        out[key] = _over(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def load(name: str, *, style: str, accent: str | None = None, words: bool = True) -> spec.SiteSpec:
    """The sample in `style`, its words the base's with that style's own laid over them, and `accent` (which must read
    on the style's button; checked by `spec.validate`); `words=False` leaves the words None for the model to write
    (`words.write`)."""
    if name not in names():
        raise ValueError(f"no sample called {name!r}; use {', '.join(names())}")
    data = json.loads((DIR / f"{name}.json").read_text(encoding="utf-8"))
    per_style = data.pop("style_words", {})
    data["style"], data["accent"] = style, accent
    if not words:
        data["words"] = None
    elif style in per_style:
        data["words"] = _over(data["words"], per_style[style])
    return spec.SiteSpec.model_validate(data)
