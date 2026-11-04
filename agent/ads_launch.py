"""Moved in M0; import from agent.channels.google_search. The plan-to-Google-operations builders are now
`agent.channels.google_search.build`; this module only keeps the old path importable for one release."""

from .channels.google_search.build import *  # moved in M0; shim kept one release
from .channels.google_search.build import _phrase  # noqa: F401 - an old importer's private name, kept one release
