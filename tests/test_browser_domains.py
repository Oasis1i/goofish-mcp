"""browser.py 纮函数：cookie 窄域注入。

有 domain 的 records 单域注入（不广播）；domain='' 的 legacy flat dict
按名字窄映射分发（淘系签名链路 → .taobao.com，其余 → .goofish.com）。
"""
from __future__ import annotations

from goofish_mcp.core.browser import _cookies_to_playwright


def test_legacy_dict_narrow_mapping():
    out = _cookies_to_playwright({"unb": "123", "_m_h5_tk": "abc_def", "x5sec": "s"})
    assert {(c["name"], c["domain"]) for c in out} == {
        ("unb", ".goofish.com"),
        ("_m_h5_tk", ".taobao.com"),
        ("x5sec", ".taobao.com"),
    }


def test_legacy_dict_skips_empty_values():
    out = _cookies_to_playwright({"unb": "123", "empty_one": ""})
    assert {c["name"] for c in out} == {"unb"}


def test_records_with_domain_injected_verbatim():
    out = _cookies_to_playwright([
        {"name": "unb", "value": "123", "domain": ".goofish.com"},
        {"name": "x5sec", "value": "s", "domain": ".taobao.com"},
    ])
    assert {(c["name"], c["domain"]) for c in out} == {
        ("unb", ".goofish.com"),
        ("x5sec", ".taobao.com"),
    }
    assert len(out) == 2  # 不广播


def test_same_name_different_domains_both_survive():
    """同名 cookie 在两域有不同值时，两条记录都存活、各回各域（回归：原 _jars_to_dict 会覆盖）。"""
    out = _cookies_to_playwright([
        {"name": "tfstk", "value": "A", "domain": ".taobao.com"},
        {"name": "tfstk", "value": "B", "domain": ".goofish.com"},
    ])
    assert len(out) == 2
    assert {(c["name"], c["value"], c["domain"]) for c in out} == {
        ("tfstk", "A", ".taobao.com"),
        ("tfstk", "B", ".goofish.com"),
    }


def test_legacy_dict_cookie_attrs():
    out = _cookies_to_playwright({"_m_h5_tk": "abc_def"})
    assert len(out) == 1
    for c in out:
        assert c["path"] == "/"
        assert c["secure"] is True
        assert c["httpOnly"] is False
        assert c["sameSite"] == "None"
        assert c["expires"] > 0
