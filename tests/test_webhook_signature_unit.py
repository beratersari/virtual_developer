"""Unit tests for verify_webhook_signature (every branch)."""

import hashlib
import hmac

from src.jira.webhook_server import verify_webhook_signature


def test_no_secret_always_true():
    assert verify_webhook_signature(b"body", None, None) is True
    assert verify_webhook_signature(b"body", "x", "") is True


def test_secret_missing_signature():
    assert verify_webhook_signature(b"body", None, "sec") is False
    assert verify_webhook_signature(b"body", "", "sec") is False


def test_secret_valid_and_invalid():
    body = b'{"a":1}'
    secret = "sec"
    good = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_webhook_signature(body, good, secret) is True
    assert verify_webhook_signature(body, "sha256=dead", secret) is False
