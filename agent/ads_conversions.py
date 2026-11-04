"""Moved in M0; import from agent.channels.google_search. The conversion actions in the owner's Google Ads account
are now `agent.channels.google_search.conversions`; this module only keeps the old path importable for one release."""

from .channels.google_search.conversions import *  # moved in M0; shim kept one release
from .channels.google_search.conversions import _upsert  # noqa: F401 - old importers keep it one release
