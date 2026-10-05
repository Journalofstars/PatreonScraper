"""Cookie / 令牌的读取、保存与转换。

内置浏览器（QtWebEngine）在登录时会把 cookie 交给这里保存，之后
``requests`` 会话从这里恢复，两者共享同一份登录状态。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from http.cookiejar import Cookie
from typing import Any, Iterable

import requests

from .config import COOKIE_FILE
from .config import write_json
import json
from pathlib import Path

# 与 Patreon 登录态相关的关键 cookie
KEY_COOKIES = ("session_id", "patreon_device_id", "current_user_id", "__cf_bm")

PATREON_DOMAINS = ("patreon.com",)


@dataclass
class CookieRecord:
    name: str
    value: str
    domain: str = ".patreon.com"
    path: str = "/"
    secure: bool = True
    expires: int | None = None
    http_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "domain": self.domain,
            "path": self.path,
            "secure": self.secure,
            "expires": self.expires,
            "http_only": self.http_only,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CookieRecord":
        return cls(
            name=str(raw.get("name", "")),
            value=str(raw.get("value", "")),
            domain=str(raw.get("domain") or ".patreon.com"),
            path=str(raw.get("path") or "/"),
            secure=bool(raw.get("secure", True)),
            expires=raw.get("expires"),
            http_only=bool(raw.get("http_only", False)),
        )


def is_patreon_domain(domain: str) -> bool:
    domain = (domain or "").lstrip(".").lower()
    return any(domain == d or domain.endswith("." + d) for d in PATREON_DOMAINS)


def save_cookies(records: Iterable[CookieRecord], path: Path | None = None) -> int:
    """保存 cookie 列表（仅 Patreon 域名），返回保存条数。"""
    unique: dict[tuple[str, str, str], CookieRecord] = {}
    for record in records:
        if not record.name or not is_patreon_domain(record.domain):
            continue
        unique[(record.name, record.domain, record.path)] = record
    payload = {
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "cookies": [r.to_dict() for r in unique.values()],
    }
    write_json(path or COOKIE_FILE, payload)
    return len(unique)


def load_cookies(path: Path | None = None) -> list[CookieRecord]:
    source = path or COOKIE_FILE
    if not source.exists():
        return []
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = raw.get("cookies") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    records: list[CookieRecord] = []
    for entry in items:
        if isinstance(entry, dict) and entry.get("name"):
            records.append(CookieRecord.from_dict(entry))
    return records


def load_saved_at(path: Path | None = None) -> str:
    source = path or COOKIE_FILE
    if not source.exists():
        return ""
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
        return str(raw.get("saved_at") or "")
    except (OSError, json.JSONDecodeError):
        return ""


def has_session(records: Iterable[CookieRecord]) -> bool:
    return any(r.name == "session_id" and r.value for r in records)


def session_id_of(records: Iterable[CookieRecord]) -> str:
    for record in records:
        if record.name == "session_id":
            return record.value
    return ""


def _to_cookiejar(records: Iterable[CookieRecord]) -> list[Cookie]:
    jar: list[Cookie] = []
    now = int(time.time())
    for record in records:
        if not record.name:
            continue
        expires = record.expires
        if expires is not None and expires > 10**11:      # 毫秒 -> 秒
            expires = int(expires / 1000)
        if expires is not None and expires <= now:
            continue
        domain = record.domain or ".patreon.com"
        jar.append(
            Cookie(
                version=0,
                name=record.name,
                value=record.value,
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=True,
                domain_initial_dot=domain.startswith("."),
                path=record.path or "/",
                path_specified=True,
                secure=bool(record.secure),
                expires=expires,
                discard=expires is None,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": ""} if record.http_only else {},
                rfc2109=False,
            )
        )
    return jar


def apply_to_session(session: requests.Session, records: Iterable[CookieRecord]) -> int:
    """把 cookie 注入 requests 会话；返回注入条数。"""
    jar = _to_cookiejar(records)
    count = 0
    for cookie in jar:
        try:
            session.cookies.set_cookie(cookie)
            count += 1
        except Exception:  # noqa: BLE001
            continue
    return count


def parse_cookie_header(text: str) -> list[CookieRecord]:
    """解析用户粘贴的 ``name=value; name2=value2`` 或整段请求头。"""
    text = (text or "").strip()
    if not text:
        return []
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1]
    text = text.replace("\r", ";").replace("\n", ";")
    records: list[CookieRecord] = []
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        name, value = name.strip(), value.strip().strip('"')
        if not name:
            continue
        records.append(CookieRecord(name=name, value=value))
    return records


def merge_cookies(
    existing: Iterable[CookieRecord], incoming: Iterable[CookieRecord]
) -> list[CookieRecord]:
    """按 (name, domain, path) 合并，新的覆盖旧的。"""
    merged: dict[tuple[str, str, str], CookieRecord] = {}
    for record in existing:
        merged[(record.name, record.domain, record.path)] = record
    for record in incoming:
        merged[(record.name, record.domain, record.path)] = record
    return list(merged.values())
