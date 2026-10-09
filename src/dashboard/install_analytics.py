"""Bearer check for the release site's Analytics read.

The dashboard password still protects ``/api/analytics``. This constant is
the same string in the release site. It is not an operator setting.
Do not log it, and do not put it on a query string.
"""

from __future__ import annotations

import hmac

# Same value as yaver_releases.install_fetch.INSTALL_ANALYTICS_TOKEN.
INSTALL_ANALYTICS_TOKEN = "c4e8a1b7d2f6093c5e8a4b1d7f0c6e9a2b5d8f1c4e7a0b3d6f9c2e5a8b1d4f70"


def install_analytics_authorized(authorization: str) -> bool:
    """True when the header is the built-in bearer. A query token does not count."""
    scheme, separator, presented = (authorization or "").partition(" ")
    if not separator or scheme.lower() != "bearer":
        return False
    try:
        got = presented.strip().encode("utf-8")
    except UnicodeError:
        return False
    expected = INSTALL_ANALYTICS_TOKEN.encode("utf-8")
    if not expected or len(got) != len(expected):
        return False
    return hmac.compare_digest(got, expected)
