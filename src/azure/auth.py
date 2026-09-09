"""Azure DevOps Server accepts PAT as a Basic password. IIS rejects an empty username."""

from __future__ import annotations

import base64


def azure_basic_user(username: str = "") -> str:
    """Username sent with a PAT. Empty becomes ``pat`` (IIS rejects blank)."""
    return (username or "").strip() or "pat"


def azure_basic_auth(token: str, username: str = "") -> str:
    """``Authorization`` value: ``Basic pat:<PAT>`` (same as Creasy)."""
    raw = f"{azure_basic_user(username)}:{(token or '').strip()}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def azure_basic_auth_header(pat: str, username: str = "") -> str:
    """Alias kept for callers that imported the header helper from the client."""
    return azure_basic_auth(pat, username)
