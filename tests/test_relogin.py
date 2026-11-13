"""自动刷新关闭时给出 needs_relogin，并且不调用浏览器。"""

from __future__ import annotations

import json

import pytest
import requests

from goofish_mcp.core.errors import AuthRequiredError, GoofishError
from goofish_mcp.core.mtop import call
from goofish_mcp.core.relogin import emit_relogin, error_payload
from goofish_mcp.core.session import Session


def _session() -> Session:
    http = requests.Session()
    http.cookies.set("_m_h5_tk", "token_1")
    return Session(http=http, unb="seller-1", tracknick="nick", device_id="dev")


class _Response:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def test_disabled_auto_refresh_does_not_open_a_browser(monkeypatch):
    monkeypatch.setenv("GOOFISH_AUTO_REFRESH_TOKEN", "0")
    session = _session()
    session.http.post = lambda *_args, **_kwargs: _Response(
        {"ret": ["FAIL_SYS_SESSION_EXPIRED::Session过期"]}
    )
    opened: list[bool] = []
    monkeypatch.setattr(
        "goofish_mcp.core.refresh.refresh_cookies_via_browser",
        lambda *_args, **_kwargs: opened.append(True) or True,
    )

    with pytest.raises(AuthRequiredError) as exc_info:
        call(session, "mtop.test", {})

    assert opened == []
    assert exc_info.value.needs_relogin is True
    assert exc_info.value.status == "needs_relogin"
    assert exc_info.value.exit_code == 77
    assert "需要重新登录" in str(exc_info.value)
    assert "FAIL_SYS_SESSION_EXPIRED" in str(exc_info.value)


def test_enabled_auto_refresh_still_tries_the_browser(monkeypatch):
    monkeypatch.delenv("GOOFISH_AUTO_REFRESH_TOKEN", raising=False)
    session = _session()
    session.http.post = lambda *_args, **_kwargs: _Response(
        {"ret": ["FAIL_SYS_TOKEN_EXOIRED::令牌过期"]}
    )
    opened: list[bool] = []

    def _refresh(*_args, **_kwargs):
        opened.append(True)
        return False

    monkeypatch.setattr("goofish_mcp.core.refresh.refresh_cookies_via_browser", _refresh)

    with pytest.raises(AuthRequiredError) as exc_info:
        call(session, "mtop.test", {})

    assert opened == [True]
    assert exc_info.value.needs_relogin is False


def test_missing_token_with_auto_refresh_off_is_relogin(monkeypatch):
    monkeypatch.setenv("GOOFISH_AUTO_REFRESH_TOKEN", "0")
    session = _session()
    session.http.cookies.clear()

    with pytest.raises(AuthRequiredError) as exc_info:
        call(session, "mtop.test", {})

    assert exc_info.value.needs_relogin is True
    assert "goofish auth login" in str(exc_info.value)


def test_auth_status_is_machine_readable(monkeypatch):
    from goofish_mcp.commands.auth import status as mod

    monkeypatch.setenv("GOOFISH_AUTO_REFRESH_TOKEN", "0")
    monkeypatch.setattr(mod.Session, "load", lambda: _session())

    def _expired(*_args, **_kwargs):
        raise AuthRequiredError("登录态已失效，需要重新登录", needs_relogin=True)

    monkeypatch.setattr(mod, "call", _expired)
    failed = mod.status()
    assert failed["valid"] is False
    assert failed["needs_relogin"] is True
    assert failed["status"] == "needs_relogin"
    assert failed["auto_refresh"] is False

    monkeypatch.setattr(mod, "call", lambda *_args, **_kwargs: {"data": {"nick": "鱼"}})
    ok = mod.status()
    assert ok == {
        "unb": "seller-1",
        "tracknick": "nick",
        "nick": "鱼",
        "valid": True,
        "needs_relogin": False,
        "status": "ok",
        "auto_refresh": False,
    }


def test_emit_relogin_writes_one_json_line(capsys):
    emit_relogin(AuthRequiredError("需要重新登录", needs_relogin=True))
    emit_relogin(AuthRequiredError("普通失效"))
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines == [
        {
            "ok": False,
            "valid": False,
            "needs_relogin": True,
            "status": "needs_relogin",
            "auto_refresh": False,
            "error": "需要重新登录",
        }
    ]


def test_mcp_error_payload_marks_relogin():
    payload = error_payload(AuthRequiredError("需要重新登录", needs_relogin=True))
    assert payload["error_type"] == "AuthRequiredError"
    assert payload["status"] == "needs_relogin"
    assert payload["needs_relogin"] is True
    other = error_payload(GoofishError("别的错误"))
    assert "needs_relogin" not in other
