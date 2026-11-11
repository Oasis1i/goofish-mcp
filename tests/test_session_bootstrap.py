"""Session.load 自动 bootstrap 行为。

目标：验证零认知负荷路径——cookies.json 不存在时，自动从 Chrome 抓。
所有真 Chrome 调用都 monkeypatch 掉。
"""
from __future__ import annotations

import json

import pytest

from goofish_mcp.core import session as session_mod
from goofish_mcp.core.crypto import decrypt_cookies
from goofish_mcp.core.errors import AuthRequiredError


@pytest.fixture
def fake_cookies_path(tmp_path, monkeypatch):
    """把 DEFAULT_COOKIE_PATH / DEVICE_CACHE_PATH 指到 tmp_path，
    避免测试互相干扰、也避免踩到用户真实的 ~/.goofish-cli。"""
    cookies = tmp_path / "cookies.json"
    device = tmp_path / "device.json"
    monkeypatch.setattr(session_mod, "DEFAULT_COOKIE_PATH", cookies)
    monkeypatch.setattr(session_mod, "DEVICE_CACHE_PATH", device)
    monkeypatch.delenv("GOOFISH_COOKIES_PATH", raising=False)
    monkeypatch.delenv("GOOFISH_NO_CHROME_BOOTSTRAP", raising=False)
    return cookies


def test_load_uses_existing_cookies_json(fake_cookies_path, monkeypatch):
    """cookies.json 已存在 → 不该触发 Chrome bootstrap。"""
    fake_cookies_path.write_text(json.dumps([
        {"name": "unb", "value": "U1"},
        {"name": "_m_h5_tk", "value": "T_xxx_1"},
        {"name": "tracknick", "value": "nick1"},
    ]))

    # bootstrap 被调用就失败
    def fail(*_args, **_kwargs):
        raise AssertionError("bootstrap 不该被触发")
    monkeypatch.setattr(session_mod, "_bootstrap_from_browser", fail)

    s = session_mod.Session.load()
    assert s.unb == "U1"
    assert s.tracknick == "nick1"
    assert s.h5_token == "T"  # _m_h5_tk 取下划线前那一段


def test_load_bootstraps_when_missing(fake_cookies_path, monkeypatch):
    """cookies.json 不存在 → 自动从浏览器抓 → 写盘，下次直接读。"""
    fake = {"unb": "U2", "_m_h5_tk": "T_99_1", "tracknick": "nick2"}
    called = {"n": 0}

    def fake_bootstrap():
        called["n"] += 1
        return "edge", fake
    monkeypatch.setattr(session_mod, "_bootstrap_from_browser", fake_bootstrap)

    s = session_mod.Session.load()
    assert s.unb == "U2"
    assert called["n"] == 1
    # 写盘了（加密格式）
    assert fake_cookies_path.exists()
    data = decrypt_cookies(fake_cookies_path.read_bytes())
    # v2 持久化为 list[Record]；legacy flat 走 _coerce_records → domain=''
    assert {r["name"]: r["value"] for r in data} == fake
    assert all(r["domain"] == "" for r in data)
    # 文件权限 0o600
    assert (fake_cookies_path.stat().st_mode & 0o777) == 0o600

    # 第二次 load 应命中缓存，不再调 bootstrap
    s2 = session_mod.Session.load()
    assert s2.unb == "U2"
    assert called["n"] == 1, "第二次不该再调 bootstrap"


def test_load_falls_back_to_auth_error_when_bootstrap_fails(fake_cookies_path, monkeypatch):
    """浏览器抓失败 → AuthRequiredError 并给出手动兜底提示。"""
    def boom():
        raise RuntimeError("all browsers failed")
    monkeypatch.setattr(session_mod, "_bootstrap_from_browser", boom)

    with pytest.raises(AuthRequiredError) as ei:
        session_mod.Session.load()
    msg = str(ei.value)
    # 错误必须指向手动兜底路径
    assert "auth login" in msg
    assert "all browsers failed" in msg


def test_load_skips_bootstrap_when_env_disabled(fake_cookies_path, monkeypatch):
    """GOOFISH_NO_CHROME_BOOTSTRAP=1 → 不自动探测浏览器，直接报错。CI 场景需要这个开关。"""
    monkeypatch.setenv("GOOFISH_NO_CHROME_BOOTSTRAP", "1")

    def fail(*_a, **_k):
        raise AssertionError("bootstrap 被触发了")
    monkeypatch.setattr(session_mod, "_bootstrap_from_browser", fail)

    with pytest.raises(AuthRequiredError) as ei:
        session_mod.Session.load()
    assert "cookie 文件不存在" in str(ei.value)


def test_load_raises_when_bootstrapped_cookies_missing_keys(fake_cookies_path, monkeypatch):
    """bootstrap 回来的 cookie 不带 unb/_m_h5_tk → 依旧报错，
    且坚决不落盘——避免半残 cookie 污染后续每次 Session.load。"""
    monkeypatch.setattr(
        session_mod, "_bootstrap_from_browser",
        lambda: ("edge", {"tracknick": "nick_only"}),
    )

    # 前置：确保出发前文件不存在（fixture 就是这样，但显式断言更稳）
    assert not fake_cookies_path.exists()

    with pytest.raises(AuthRequiredError):
        session_mod.Session.load()

    # 关键：失败不落盘。锁住回归——以前实现是先写再校验，半残 cookie 会污染磁盘。
    assert not fake_cookies_path.exists()


def test_load_raises_when_bootstrap_fail_does_not_write(fake_cookies_path, monkeypatch):
    """bootstrap 整体抛异常时同样不能落盘。补漏——确保错误路径两种都安全。"""
    def boom():
        raise RuntimeError("keychain denied")
    monkeypatch.setattr(session_mod, "_bootstrap_from_browser", boom)

    assert not fake_cookies_path.exists()
    with pytest.raises(AuthRequiredError):
        session_mod.Session.load()
    assert not fake_cookies_path.exists()


def test_write_cookies_json_format(tmp_path):
    """write_cookies_json 应写加密格式，解密后内容正确。"""
    target = tmp_path / "out.json"
    session_mod.write_cookies_json(target, {"a": "1", "b": "2"})
    raw = target.read_bytes()
    # 加密文件不应是明文 JSON
    with pytest.raises((json.JSONDecodeError, UnicodeDecodeError)):
        json.loads(raw)
    # 解密后内容正确（v2 持久化为 list[Record]，flat dict 经 _coerce_records 转 records）
    data = decrypt_cookies(raw)
    assert {r["name"]: r["value"] for r in data} == {"a": "1", "b": "2"}
    assert (target.stat().st_mode & 0o777) == 0o600


def test_cookie_records_preserves_domain_from_bootstrap(tmp_path, monkeypatch):
    """bootstrap 走 browser_cookie.py → records 带 domain，Session.cookie_records 保留之。"""
    cookies_path = tmp_path / "cookies.json"
    device_path = tmp_path / "device.json"
    monkeypatch.setattr(session_mod, "DEFAULT_COOKIE_PATH", cookies_path)
    monkeypatch.setattr(session_mod, "DEVICE_CACHE_PATH", device_path)
    monkeypatch.delenv("GOOFISH_COOKIES_PATH", raising=False)

    # records 形态：unb/tracknick 在 .goofish.com，_m_h5_tk 在 .taobao.com
    fake_records = [
        {"name": "unb", "value": "U9", "domain": ".goofish.com"},
        {"name": "tracknick", "value": "nick9", "domain": ".goofish.com"},
        {"name": "_m_h5_tk", "value": "T_x_1", "domain": ".taobao.com"},
    ]
    monkeypatch.setattr(
        session_mod, "_bootstrap_from_browser",
        lambda: ("edge", fake_records),
    )

    # 第一次 load 触发 bootstrap → 落盘
    session_mod.Session.load()
    # 落盘后 decrypt 出来应是 records（v2 持久化）
    data = decrypt_cookies(cookies_path.read_bytes())
    assert isinstance(data, list)
    # 重新 load 走加密文件路径时 domain 也应保留
    s2 = session_mod.Session.load()
    by_name_domain = {(r["name"], r["domain"]): r["value"] for r in s2.cookie_records}
    assert by_name_domain[("unb", ".goofish.com")] == "U9"
    assert by_name_domain[("_m_h5_tk", ".taobao.com")] == "T_x_1"


def test_legacy_flat_dict_decrypts_to_domain_empty_records(tmp_path, monkeypatch):
    """旧版 flat dict cookies.json 仍能 load；records 形式为 domain=''.（兼容性回归）"""
    cookies_path = tmp_path / "cookies.json"
    device_path = tmp_path / "device.json"
    monkeypatch.setattr(session_mod, "DEFAULT_COOKIE_PATH", cookies_path)
    monkeypatch.setattr(session_mod, "DEVICE_CACHE_PATH", device_path)
    monkeypatch.delenv("GOOFISH_COOKIES_PATH", raising=False)

    # 写入 legacy 加密 flat dict
    session_mod.write_cookies_json(cookies_path, {"unb": "UL", "_m_h5_tk": "TL_x_1"})

    s = session_mod.Session.load()
    assert s.unb == "UL"
    # 旧文件没有 domain 信息，所以 records 全部 domain=''
    assert all(r["domain"] == "" for r in s.cookie_records)
    assert {r["name"] for r in s.cookie_records} == {"unb", "_m_h5_tk"}


def test_plaintext_migration_to_encrypted(fake_cookies_path, monkeypatch):
    """旧版明文 JSON 文件应被自动加密覆盖（向后兼容迁移）。"""
    # 写入旧版明文格式
    fake_cookies_path.write_text(json.dumps([
        {"name": "unb", "value": "U1"},
        {"name": "_m_h5_tk", "value": "T_xxx_1"},
    ]))
    # 确认当前是明文
    assert fake_cookies_path.read_text().startswith("[")

    monkeypatch.setattr(session_mod, "_bootstrap_from_browser", lambda: (_ for _ in ()).throw(AssertionError("不该触发")))

    s = session_mod.Session.load()
    assert s.unb == "U1"

    # 文件已被加密
    raw = fake_cookies_path.read_bytes()
    with pytest.raises((json.JSONDecodeError, UnicodeDecodeError)):
        json.loads(raw)
    data = decrypt_cookies(raw)
    flat = {r["name"]: r["value"] for r in data}
    assert flat["unb"] == "U1"
