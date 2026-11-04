"""The channel framework (spec 2026-09-29-admiry-channels-design §5): every place the ads run is one adapter behind
one protocol. `base` holds the shared types and the `Adapter` protocol; `registry` the channel table (ids, the names
the owner sees, the order on cards, the click-id kinds, the execution switch per channel, the account rows). Each
channel's adapter is a sub-package (`google_search/`, `newsbreak/`, ...) that this package imports, in
`registry.ORDER`, once it ships, so `registry.CHANNELS` holds the shipped adapters whenever this package is imported
(and importing `agent.channels.registry` imports the package first)."""

from .google_search import adapter as _google_search  # noqa: F401 - importing an adapter registers it
from .newsbreak import adapter as _newsbreak  # noqa: F401 - importing an adapter registers it
