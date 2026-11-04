"""One Jinja environment for every owner-facing page (autoescape on for .html)."""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=select_autoescape(["html"]))


def capfirst(text: str) -> str:
    """The first letter upper-cased, the rest untouched (Jinja's `capitalize` lowers the rest)."""
    return text[:1].upper() + text[1:]


def sentence(text: str) -> str:
    """An owner sentence shown on its own (a banner, a warning): the module constants (claims.OTHER_ACCOUNT,
    google_ads_link.UNREACHABLE, ...) start lowercase and end bare so that they also read mid-sentence."""
    text = capfirst(text.strip())
    return text if not text or text[-1] in ".!?" else text + "."


env.filters["capfirst"] = capfirst
env.filters["sentence"] = sentence
