"""auth login 命令的参数组合与报错语义。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from goofish_mcp.cli import app
from goofish_mcp.commands.auth import login as login_mod
from goofish_mcp.core import session as session_mod
from goofish_mcp.core.crypto import decrypt_cookies


@pytest.fixture
def fake_target(tmp_path, monkeypatch):
    """把写盘目标指到 tmp_path，避免踩到用户真实 ~/.goofish-cli/cookies.json。"""
    target = tmp_path / "cookies.json"
    monkeypatch.delenv("GOOFISH_COOKIES_PATH", raising=False)
    monkeypatch.setattr(session_mod, "DEFAULT_COOKIE_PATH", target)
    monkeypatch.setattr(session_mod, "DEVICE_CACHE_PATH", tmp_path / "device.json")
    return target


def test_raw_without_source_raises(fake_target, monkeypatch):
    """goofish auth login --raw（没传 cookie 字符串）→ 必须报参数错，
    不能静默走浏览器 auto——否则 --raw 标志被吞，用户会困惑为什么没生效。"""
    # bootstrap 被调就失败（证明没走 auto 分支）
    def boom(*_args, **_kwargs):
        raise AssertionError("不该触发浏览器 auto")
    monkeypatch.setattr(login_mod, "_pull_from_browser", boom)

    with pytest.raises(ValueError) as ei:
        login_mod.login(source=None, raw=True)
    assert "--raw" in str(ei.value)


def test_no_args_goes_to_browser_auto(fake_target, monkeypatch):
    """goofish auth login 无参数 → 走浏览器 auto-detect。"""
    fake = [
        {"name": "unb", "value": "U", "domain": "", "path": ""},
        {"name": "_m_h5_tk", "value": "T_xxx_1", "domain": "", "path": ""},
        {"name": "tracknick", "value": "n", "domain": "", "path": ""},
    ]
    monkeypatch.setattr(login_mod, "_pull_from_browser", lambda b: (fake, f"browser:{b}"))

    out = login_mod.login()
    assert out["source"] == "browser:auto"
    assert out["unb"] == "U"
    assert fake_target.exists()


def test_raw_with_source_parses(fake_target, monkeypatch):
    """--raw 有 cookie 字符串 → 正确解析，不走浏览器。"""
    def boom(*_args, **_kwargs):
        raise AssertionError("不该触发浏览器 auto")
    monkeypatch.setattr(login_mod, "_pull_from_browser", boom)

    out = login_mod.login(
        source="unb=U; _m_h5_tk=T_x_1; tracknick=n",
        raw=True,
    )
    assert out["source"] == "raw"
    assert out["unb"] == "U"


def test_file_path_import(fake_target, tmp_path, monkeypatch):
    """位置参数 <path> → 从 JSON 文件导入。"""
    src = tmp_path / "src.json"
    src.write_text(json.dumps([
        {"name": "unb", "value": "U"},
        {"name": "_m_h5_tk", "value": "T_x_1"},
    ]))

    def boom(*_args, **_kwargs):
        raise AssertionError("不该触发浏览器 auto")
    monkeypatch.setattr(login_mod, "_pull_from_browser", boom)

    out = login_mod.login(source=str(src))
    assert out["source"].startswith("file:")
    assert out["unb"] == "U"


def test_file_path_is_accepted_as_cli_argument(fake_target, tmp_path):
    src = tmp_path / "src.json"
    src.write_text(json.dumps([
        {"name": "unb", "value": "U"},
        {"name": "_m_h5_tk", "value": "T_x_1"},
    ]))

    result = CliRunner().invoke(
        app,
        ["auth", "login", str(src), "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    assert fake_target.exists()


def test_qr_login_preserves_domain_and_path_in_persisted_records(fake_target, monkeypatch):
    """review-3 P1-B：QR 登录返回的 CookieRecord 必须保留 domain/path，
    不能退化为 flat dict 后再 coerce——否则写盘后 domain="" path="/"。

    通过 `auth login --qr` 端到端验证 persisted cookies.json 内容。
    """
    fake_records = [
        {"name": "_m_h5_tk", "value": "tk_abc", "domain": ".taobao.com", "path": "/"},
        {"name": "unb", "value": "U12345", "domain": ".goofish.com", "path": "/"},
        {"name": "cookie2", "value": "c2_val", "domain": ".taobao.com", "path": "/"},
        {"name": "tracknick", "value": "nicky", "domain": ".goofish.com", "path": "/account"},
    ]

    # login_via_qr 在 login.py 里是局部 import，patch 底层模块而非引用名
    monkeypatch.setattr(
        "goofish_mcp.core.qr_login.login_via_qr",
        lambda **_: fake_records,
    )

    out = login_mod.login(qr=True)
    assert out["source"] == "qr"
    assert out["unb"] == "U12345"

    # 磁盘里是加密格式，解密后验证 domain / path 完整
    persisted = decrypt_cookies(fake_target.read_bytes())
    assert isinstance(persisted, list)
    domains = {r["domain"] for r in persisted}
    assert ".taobao.com" in domains, "QR cookie 应保留 .taobao.com domain"
    assert ".goofish.com" in domains, "QR cookie 应保留 .goofish.com domain"
    # tracknick 有非根 path
    track_rec = next((r for r in persisted if r["name"] == "tracknick"), None)
    assert track_rec is not None
    assert track_rec["path"] == "/account", "QR cookie 应保留非根 path /account"


def test_login_writes_to_goofish_cookies_path(tmp_path, monkeypatch):
    """auth login 必须走 resolve_cookie_path()，尊重 GOOFISH_COOKIES_PATH。"""
    custom = tmp_path / "alt" / "cookies.json"
    default = tmp_path / "default" / "cookies.json"
    monkeypatch.setattr(session_mod, "DEFAULT_COOKIE_PATH", default)
    monkeypatch.setattr(session_mod, "DEVICE_CACHE_PATH", tmp_path / "device.json")
    monkeypatch.setenv("GOOFISH_COOKIES_PATH", str(custom))

    fake = [
        {"name": "unb", "value": "U", "domain": "", "path": ""},
        {"name": "_m_h5_tk", "value": "T_xxx_1", "domain": "", "path": ""},
        {"name": "tracknick", "value": "n", "domain": "", "path": ""},
    ]
    monkeypatch.setattr(login_mod, "_pull_from_browser", lambda b: (fake, f"browser:{b}"))

    out = login_mod.login()
    assert Path(out["path"]) == custom
    assert custom.exists()
    assert not default.exists()
