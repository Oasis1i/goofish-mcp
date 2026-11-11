"""测 refresh 模块。Playwright 走真浏览器不进单测，mock 掉 asyncio.run。"""
from __future__ import annotations

from unittest.mock import patch

import pytest
import requests

from goofish_mcp.core import refresh
from goofish_mcp.core.session import Session


def _make_session(cookies: dict[str, str]) -> Session:
    http = requests.Session()
    http.cookies.update(cookies)
    return Session(http=http, unb=cookies.get("unb", ""), tracknick="", device_id="dev")


def _make_session_with_records(records: list[dict[str, str]]) -> Session:
    """造一个带 cookie_records 的 Session（review-3 复现老路径必备）。"""
    http = requests.Session()
    return Session(
        http=http,
        unb=next((r["value"] for r in records if r["name"] == "unb"), ""),
        tracknick="",
        device_id="dev",
        cookie_records=list(records),
    )


def _rec(name: str, value: str, domain: str = ".goofish.com") -> dict[str, str]:
    return {"name": name, "value": value, "domain": domain}


def _fake_run(result):
    """模拟 asyncio.run：close 掉传入的 coroutine（避免 unawaited warning）后返回结果。"""
    def _inner(coro):
        coro.close()
        return result
    return _inner


def _fake_run_raises(exc):
    def _inner(coro):
        coro.close()
        raise exc
    return _inner


def test_refresh_merges_new_cookies_and_dedupes_same_name(tmp_path, monkeypatch):
    """关键回归：刷新后不能出现跨 domain 同名 cookie（`.cookies.get(...)` 会抛）。"""
    # 预埋一份旧 _m_h5_tk（默认 domain）
    session = _make_session({"_m_h5_tk": "old_1", "unb": "u1", "cookie2": "c2"})

    # 通过 GOOFISH_COOKIES_PATH 让 resolve_cookie_path 指到测试临时路径
    monkeypatch.setenv("GOOFISH_COOKIES_PATH", str(tmp_path / "cookies.json"))

    fresh = [_rec("_m_h5_tk", "new_2"), _rec("unb", "u1"), _rec("cookie2", "c3"), _rec("x5sec", "x")]
    with patch.object(refresh, "asyncio") as mock_async:
        mock_async.run.side_effect = _fake_run(fresh)
        ok = refresh.refresh_cookies_via_browser(session)

    assert ok is True
    # 去重后能正常 get —— 不会抛 "multiple cookies with name"
    assert session.http.cookies.get("_m_h5_tk") == "new_2"
    assert session.http.cookies.get("cookie2") == "c3"
    assert session.http.cookies.get("x5sec") == "x"
    # 磁盘也写了
    assert (tmp_path / "cookies.json").exists()


def test_refresh_fails_when_required_keys_missing(tmp_path, monkeypatch):
    session = _make_session({"_m_h5_tk": "old", "unb": "u1"})
    monkeypatch.setenv("GOOFISH_COOKIES_PATH", str(tmp_path / "cookies.json"))

    with patch.object(refresh, "asyncio") as mock_async:
        mock_async.run.side_effect = _fake_run([_rec("foo", "bar")])  # 没 _m_h5_tk / unb
        ok = refresh.refresh_cookies_via_browser(session)

    assert ok is False
    # 不应覆盖旧 session
    assert session.http.cookies.get("_m_h5_tk") == "old"
    # 也不应写盘
    assert not (tmp_path / "cookies.json").exists()


def test_refresh_respects_custom_cookie_path(tmp_path, monkeypatch):
    """GOOFISH_COOKIES_PATH 自定义路径必须被尊重（Copilot review feedback）。"""
    custom = tmp_path / "nested" / "my.json"
    monkeypatch.setenv("GOOFISH_COOKIES_PATH", str(custom))

    session = _make_session({"_m_h5_tk": "old", "unb": "u1"})
    fresh = [_rec("_m_h5_tk", "new"), _rec("unb", "u1"), _rec("cookie2", "c")]
    with patch.object(refresh, "asyncio") as mock_async:
        mock_async.run.side_effect = _fake_run(fresh)
        ok = refresh.refresh_cookies_via_browser(session)

    assert ok is True
    assert custom.exists()
    # 默认路径不应被写
    assert not (tmp_path / "cookies.json").exists()


def test_refresh_fails_when_cookie2_missing(tmp_path, monkeypatch):
    """cookie2 是真正的 session token，缺它必须判失败（v0.2.3 Copilot review）。"""
    session = _make_session({"_m_h5_tk": "old", "unb": "u1", "cookie2": "old_c"})
    monkeypatch.setenv("GOOFISH_COOKIES_PATH", str(tmp_path / "cookies.json"))

    # fresh 有 _m_h5_tk / unb 但无 cookie2 —— 过去会误判成功，现在必须 False
    fresh = [_rec("_m_h5_tk", "new"), _rec("unb", "u1")]
    with patch.object(refresh, "asyncio") as mock_async:
        mock_async.run.side_effect = _fake_run(fresh)
        ok = refresh.refresh_cookies_via_browser(session)

    assert ok is False
    # 旧 cookie 不能被覆盖
    assert session.http.cookies.get("_m_h5_tk") == "old"
    assert not (tmp_path / "cookies.json").exists()


def test_refresh_swallows_playwright_exception():
    session = _make_session({"_m_h5_tk": "old", "unb": "u1"})
    with patch.object(refresh, "asyncio") as mock_async:
        mock_async.run.side_effect = _fake_run_raises(RuntimeError("chrome not installed"))
        ok = refresh.refresh_cookies_via_browser(session, persist=False)
    assert ok is False


def test_refresh_persist_false_success_assigns_merged_to_memory(tmp_path, monkeypatch):
    """review-3 P1-A：persist=False 成功时必须把 merged 写回 session.cookie_records，
    不能 UnboundLocalError，也不能丢无关老记录。
    """
    custom = tmp_path / "no_write.json"
    monkeypatch.setenv("GOOFISH_COOKIES_PATH", str(custom))

    # 内存里老 records：unb + 一个无关 cookie `keep_old`
    old_records = [
        _rec("unb", "u1"),
        _rec("_m_h5_tk", "old_1"),
        _rec("cookie2", "old_c"),
        _rec("keep_old", "kept"),  # 不在 fresh 里，合并后应保留
    ]
    session = _make_session_with_records(old_records)
    # http.cookies 也得有 old 让 refresh 的 jar 走通
    session.http.cookies.update(
        {"unb": "u1", "_m_h5_tk": "old_1", "cookie2": "old_c", "keep_old": "kept"}
    )

    # fresh 模拟 Playwright 拿到的全新快照：三个必需 + 新增 x5sec
    fresh = [
        _rec("unb", "u1"),
        _rec("_m_h5_tk", "new_2", ".taobao.com"),
        _rec("cookie2", "new_c", ".taobao.com"),
        _rec("x5sec", "x", ".goofish.com"),
    ]
    with patch.object(refresh, "asyncio") as mock_async:
        mock_async.run.side_effect = _fake_run(fresh)
        ok = refresh.refresh_cookies_via_browser(session, persist=False)

    # 成功 + 内存一致 + 无关老记录被保留
    assert ok is True
    # 内存 records 应是 merged：old 留下的 (unb / keep_old) + fresh
    names = [r["name"] for r in session.cookie_records]
    assert "keep_old" in names, "无关老记录 keep_old 必须被保留在内存里"
    assert "_m_h5_tk" in names
    assert "x5sec" in names
    # 不能写盘（persist=False）
    assert not custom.exists()


@pytest.mark.parametrize(
    "env,expected",
    [(None, True), ("0", False), ("1", True), ("", True)],
)
def test_is_enabled_honors_env(monkeypatch, env, expected):
    if env is None:
        monkeypatch.delenv("GOOFISH_AUTO_REFRESH_TOKEN", raising=False)
    else:
        monkeypatch.setenv("GOOFISH_AUTO_REFRESH_TOKEN", env)
    assert refresh.is_enabled() is expected
