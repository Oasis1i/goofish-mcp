"""发送前外联词和转账词扫描。

词表读 skills/goofish-risk-guard/references/external-contact-keywords.md 的平台表和
诱导动作列表。支付宝、转账、手机号、QQ 号按同一份说明里的例子和正则补上。
词表缺失或为空时拒绝发送，不放行。
"""

from __future__ import annotations

import re
from pathlib import Path

from goofish_mcp.core.errors import GoofishError

_PAYMENT_WORDS = ("支付宝", "转账", "转我", "收款码", "付款码", "二维码", "线下交易", "红包")
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_QQ = re.compile(
    r"(?:QQ|Q号|q号|扣扣|企鹅号).{0,8}[1-9]\d{4,11}"
    r"|[1-9]\d{4,11}.{0,8}(?:QQ|Q号|q号|扣扣|企鹅号)"
    r"|(?<![A-Za-z0-9])[Qq]{1,2}(?![A-Za-z])[\s:：]{0,3}[1-9]\d{4,11}",
)
_COMBOS = (
    re.compile(r"方便.{0,8}加"),
    re.compile(r"详情.{0,4}加"),
    re.compile(r"手机.{0,4}联系"),
    re.compile(r"直接.{0,6}转"),
)


def keyword_file() -> Path:
    from goofish_mcp.commands.skills.install import _bundle_root

    return _bundle_root() / "goofish-risk-guard" / "references" / "external-contact-keywords.md"


def load_keywords(text: str) -> tuple[str, ...]:
    """只取平台表和「诱导动作」列表，不把灰色区里的单字（方便/直接）当禁词。"""
    head = text.split("## 合规替换", 1)[0]
    platform, _, rest = head.partition("### 诱导动作类")
    actions = rest.split("### ", 1)[0] if rest else ""
    words: list[str] = []
    for line in platform.splitlines():
        if not line.startswith("|") or "---" in line or "原词" in line:
            continue
        for cell in line.strip().strip("|").split("|"):
            words.extend(_tokens(cell))
    for line in actions.splitlines():
        if line.startswith("- "):
            words.extend(_tokens(line[2:]))
    unique = list(dict.fromkeys(words))
    unique.sort(key=len, reverse=True)
    return tuple(unique)


def scan_text(text: str, *, path: Path | None = None) -> list[dict[str, str]]:
    if not text:
        return []
    hits: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(kind: str, word: str) -> None:
        if word in seen:
            return
        seen.add(word)
        hits.append({"kind": kind, "word": word})

    for word in _keywords(path):
        if _contains(text, word):
            add("keyword", word)
            break
    for pattern in _COMBOS:
        match = pattern.search(text)
        if match:
            add("combo", match.group(0))
            break
    if _PHONE.search(text):
        add("phone", "手机号")
    if _QQ.search(text):
        add("qq", "QQ号")
    for word in _PAYMENT_WORDS:
        if word in text:
            add("payment", word)
            break
    return hits


def _keywords(path: Path | None) -> tuple[str, ...]:
    source = path or keyword_file()
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise GoofishError(f"找不到外联词表：{source}。发送已停止。") from exc
    words = load_keywords(text)
    if not words:
        raise GoofishError(f"外联词表是空的：{source}。发送已停止。")
    return words


def _tokens(cell: str) -> list[str]:
    parts = re.split(r"\s*/\s*|\s*、\s*", cell.strip())
    tokens: list[str] = []
    for part in parts:
        token = part.strip().strip("`").strip("\"“”")
        if not token or "XX" in token or token == "X":
            continue
        if len(token) < 2 or len(token) > 16:
            continue
        tokens.append(token)
    return tokens


def _contains(text: str, word: str) -> bool:
    if word.isascii():
        if len(word) <= 3:
            return (
                re.search(
                    rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])",
                    text,
                    re.IGNORECASE,
                )
                is not None
            )
        return word.casefold() in text.casefold()
    return word in text
