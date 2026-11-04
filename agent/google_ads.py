"""Moved in M0; import from agent.channels.google_search. The Google Ads REST client is now
`agent.channels.google_search.client`; this module only keeps the old path importable for one release."""

from .channels.google_search.client import *  # moved in M0; shim kept one release
from .channels.google_search.client import (  # noqa: F401 - an old importer's private names, kept one release
    _access_token,
    _call,
    _codes,
    _failure,
    _json,
    _session,
)
