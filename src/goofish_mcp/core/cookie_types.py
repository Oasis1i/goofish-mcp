"""跨模块 cookie 记录契约。TypedDict 作为跨模块类型锚点，避免循环 import。"""
from typing import TypedDict


class CookieRecord(TypedDict):
    """Cookie 记录的标准形态。domain/path 缺失时为空串（legacy 标记）。"""
    name: str
    value: str
    domain: str
    path: str
