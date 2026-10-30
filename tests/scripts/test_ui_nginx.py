"""Guards on ui/nginx.conf: the Grafana embed must stay authenticated, header-trusted and free of the reload loop."""

import re
from pathlib import Path

CONF = (Path(__file__).resolve().parents[2] / "ui" / "nginx.conf").read_text(encoding="utf-8")


def block(location: str) -> str:
    start = CONF.index(location)
    return CONF[start : CONF.index("\n  }", start)]


def test_grafana_requires_a_signed_in_ui_session_and_sets_its_own_identity_header() -> None:
    grafana = block("location /grafana/ {")
    assert "auth_request /_session_check;" in grafana
    assert 'proxy_set_header X-WEBAUTH-USER "ui-viewer";' in grafana  # set here, so a browser cannot choose it
    assert 'proxy_set_header Cookie "";' in grafana  # the UI session cookie never reaches Grafana


def test_the_session_check_is_internal_only() -> None:
    assert "internal;" in block("location = /_session_check {")


def test_token_rotation_is_answered_locally_so_the_embedded_page_cannot_reload_in_a_loop() -> None:
    rotate = block("location = /grafana/api/user/auth-tokens/rotate {")
    assert "return 200" in rotate and "proxy_pass" not in rotate


def test_the_app_keeps_a_strict_policy_and_only_the_grafana_path_relaxes_it() -> None:
    assert "frame-src 'self'" in CONF and "frame-ancestors 'self'" in CONF
    assert len(re.findall(r"default-src 'self'", CONF)) == 1  # the strict policy is set once, for the app
