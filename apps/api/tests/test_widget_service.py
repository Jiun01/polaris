"""Off-Meta channels M3 — widget service unit tests (allowlist is security-critical)."""

from __future__ import annotations

import pytest
from polaris_api.services import widget


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://t.me/acme_bot", True),
        ("https://wa.me/60123456789", True),
        ("https://m.me/acmepage", True),
        ("https://ig.me/m/acme", True),
        ("http://t.me/acme_bot", False),  # not https
        ("https://evil.com/phish", False),  # not allowlisted
        ("https://t.me.evil.com/x", False),  # lookalike host
        ("https://evil.com@t.me/x", False),  # userinfo open-redirect bypass
        ("https://t.me@evil.com/x", False),  # host is evil.com after '@'
        ("https://t.me:8443/x", False),  # explicit non-443 port
        ("javascript:alert(1)", False),
        ("", False),
        ("ftp://t.me/x", False),
    ],
)
def test_is_allowed_target(url: str, ok: bool) -> None:
    assert widget.is_allowed_target(url) is ok


def test_gen_widget_key_is_urlsafe_and_unique() -> None:
    a, b = widget.gen_widget_key(), widget.gen_widget_key()
    assert a != b
    assert all(c.isalnum() or c in "-_" for c in a)
