"""回复决定的审计日志。追加 JSONL，手机号等长数字会打码。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from filelock import FileLock

from goofish_mcp.core.inbox import resolve_audit_path


def redact(text: str) -> str:
    cleaned = re.sub(r"\d{5,}", lambda match: "#" * len(match.group(0)), text or "")
    return cleaned[:80]


def append_audit(record: dict[str, Any], path: str | Path | None = None) -> Path:
    target = resolve_audit_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    safe = dict(record)
    for key in ("text", "buyer_text", "draft", "send_message"):
        if key in safe and isinstance(safe[key], str):
            safe[key] = redact(safe[key])
    line = json.dumps(safe, ensure_ascii=False, default=str) + "\n"
    with FileLock(str(target) + ".lock", timeout=5, mode=0o600):
        fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(target, 0o600)
    return target
