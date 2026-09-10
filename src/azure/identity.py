"""TFS / Azure identity for @mention matching (Creasy connectionData seed).

``GET {identity_root}/_apis/connectionData`` — not collection-scoped.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

import httpx

from src.azure.auth import azure_basic_auth
from src.azure.log import azure_info, azure_warning
from src.azure.urls import identity_root, identity_roots
from src.gitlab.mentions import identity_key, normalize_guid, normalize_mention

_CACHE: Dict[str, Dict[str, Any]] = {}


def identity_name_values(user: Dict[str, Any]) -> List[str]:
    """Display / account names from connectionData or an identities row."""
    names: List[str] = []

    def add(raw: Any) -> None:
        value = raw
        if isinstance(value, dict):
            value = value.get("$value") or value.get("value") or ""
        text = str(value or "").strip()
        if text and text not in names:
            names.append(text)

    for key in (
        "providerDisplayName",
        "displayName",
        "customDisplayName",
        "uniqueName",
        "directoryAlias",
        "mailAddress",
        "principalName",
    ):
        add(user.get(key))
    props = user.get("properties")
    if isinstance(props, dict):
        for key in (
            "Account",
            "AccountName",
            "DirectoryAlias",
            "SamAccountName",
            "Mail",
            "MailAddress",
        ):
            add(props.get(key))
    return names


def parse_authenticated_user(data: Any) -> Optional[Dict[str, Any]]:
    blob = data if isinstance(data, dict) else {}
    user = blob.get("authenticatedUser")
    if not isinstance(user, dict):
        user = blob if blob.get("id") else None
    if not isinstance(user, dict) or not user.get("id"):
        return None
    uid = str(user.get("id") or "").strip()
    if not uid:
        return None
    return {"id": uid, "names": identity_name_values(user)}


def seed_identity_aliases(
    configured: Iterable[str], identity: Optional[Dict[str, Any]]
) -> List[str]:
    """Add PAT user id/names only when they match AZURE_TRIGGER_USER."""
    extra: List[str] = []
    if not identity:
        return extra
    bots = {
        identity_key(x) or normalize_mention(x)
        for x in configured or []
        if identity_key(x) or normalize_mention(x)
    }
    bots.update(g for x in (configured or []) if (g := normalize_guid(str(x))))
    if not bots:
        return extra
    uid = str(identity.get("id") or "").strip()
    names = [str(n).strip() for n in (identity.get("names") or []) if str(n).strip()]
    keys = {identity_key(n) or normalize_mention(n) for n in names}
    keys.discard("")
    gid = normalize_guid(uid)
    if gid in bots or keys.intersection(bots):
        if uid:
            extra.append(uid)
        extra.extend(names)
    return extra


def reviewer_bot_aliases(
    pr: Dict[str, Any],
    configured: Iterable[str],
    *,
    bot_id: str = "",
) -> List[str]:
    """Ids and names of PR reviewers who are the configured bot."""
    extra: List[str] = []
    bots = {
        identity_key(x) or normalize_mention(x)
        for x in configured or []
        if identity_key(x) or normalize_mention(x)
    }
    bots.update(g for x in (configured or []) if (g := normalize_guid(str(x))))
    want_id = (normalize_guid(bot_id) or str(bot_id or "").strip().lower())
    reviewers = pr.get("reviewers") if isinstance(pr.get("reviewers"), list) else []
    for row in reviewers:
        if not isinstance(row, dict):
            continue
        nested = row.get("user") if isinstance(row.get("user"), dict) else {}
        ids = [
            str(row.get("id") or "").strip(),
            str((nested or {}).get("id") or "").strip(),
        ]
        names = identity_name_values(row) + identity_name_values(nested or {})
        keys = {identity_key(n) or normalize_mention(n) for n in names}
        keys.discard("")
        id_hit = bool(want_id) and any(
            (normalize_guid(i) or i.lower()) == want_id for i in ids if i
        )
        if not id_hit and not keys.intersection(bots):
            continue
        extra.extend(i for i in ids if i)
        extra.extend(names)
    return extra


def fetch_bot_identity(
    *,
    host: str = "",
    collection_url: str = "",
    pat: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Cached ``connectionData`` user at the TFS identity root (not collection)."""
    from src.config import settings

    roots: List[str] = []
    for raw in (collection_url, host):
        text = str(raw or "").strip()
        if not text:
            continue
        if "://" not in text and "." not in text and ":" not in text:
            continue
        if "://" not in text:
            local = text.startswith("127.") or text.startswith("localhost")
            text = f"{'http' if local else 'https'}://{text}"
        roots.extend(identity_roots(identity_root(text) or text))
    token = (pat or "").strip()
    if not token and host and hasattr(settings, "azure_pat_for_host"):
        parsed = urlparse(host if "://" in host else f"https://{host}")
        h = (parsed.hostname or host).lower()
        token = (settings.azure_pat_for_host(h) or "").strip()
    cache_key = f"{'|'.join(roots)}|{bool(token)}"
    if cache_key in _CACHE:
        return _CACHE[cache_key] or None
    if not roots or not token:
        _CACHE[cache_key] = {}
        return None
    headers = {
        "Accept": "application/json",
        "Authorization": azure_basic_auth(token),
    }
    try:
        with httpx.Client(timeout=20.0, verify=False, headers=headers) as client:
            for base in roots:
                url = f"{base.rstrip('/')}/_apis/connectionData"
                for ver in ("7.1", "7.0", "6.0", "1.0"):
                    try:
                        resp = client.get(url, params={"api-version": ver})
                    except httpx.HTTPError:
                        resp = None
                    if resp is None:
                        break
                    if resp.status_code in (400, 404):
                        continue
                    if resp.status_code != 200:
                        break
                    user = parse_authenticated_user(
                        resp.json() if resp.content else {}
                    )
                    if user:
                        azure_info(
                            f"mention identity id={user.get('id')} "
                            f"names={user.get('names')} root={base}"
                        )
                        _CACHE[cache_key] = user
                        return user
    except Exception as exc:
        azure_warning(f"mention identity lookup failed: {exc}")
    _CACHE[cache_key] = {}
    return None


def reset_identity_cache() -> None:
    _CACHE.clear()
