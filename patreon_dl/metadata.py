"""保存作品正文与元数据（方便离线归档与检索）。"""

from __future__ import annotations

import html
import json
import os
import re
from typing import Iterable

from .config import AppConfig
from .models import Campaign, MediaItem, PostItem

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"</(p|div|h[1-6]|li|tr)\s*>", re.IGNORECASE)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")


def html_to_text(content: str | None) -> str:
    """把 Patreon 的正文 HTML 粗略转成纯文本。"""
    if not content:
        return ""
    text = str(content)
    text = _BR_RE.sub("\n", text)
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = _MULTI_SPACE_RE.sub(" ", text)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    return text.strip()


def _safe_write(path: str, text: str) -> str:
    target = path
    if os.name == "nt" and len(os.path.abspath(path)) >= 240:
        target = "\\\\?\\" + os.path.abspath(path)
    with open(target, "w", encoding="utf-8", errors="replace") as handle:
        handle.write(text)
    return path


def write_post_files(
    config: AppConfig,
    campaign: Campaign,
    post: PostItem,
    directory: str,
    media: Iterable[MediaItem] | None = None,
) -> list[str]:
    """在作品目录内写入 post.json / post.txt，返回写出的文件路径。"""
    os.makedirs(directory, exist_ok=True)
    written: list[str] = []

    if config.write_post_text:
        body = html_to_text(post.summary)
        header = [
            f"标题: {post.safe_title}",
            f"发布时间: {post.date_text}",
            f"链接: {post.url}",
            f"作品类型: {post.post_type}",
            "",
            "-" * 60,
            "",
        ]
        text = "\n".join(header) + (body or "(无正文)")
        written.append(_safe_write(os.path.join(directory, "post.txt"), text))

    if config.write_metadata:
        items = list(media) if media is not None else list(post.media)
        payload = {
            "campaign": {
                "id": campaign.id,
                "name": campaign.display_name,
                "vanity": campaign.vanity,
                "url": campaign.url,
            },
            "post": {
                "id": post.id,
                "title": post.safe_title,
                "published_at": post.published_at,
                "edited_at": post.edited_at,
                "post_type": post.post_type,
                "url": post.url,
                "is_paid": post.is_paid,
                "current_user_can_view": post.can_view,
                "min_cents_pledged_to_view": post.min_cents,
                "teaser_text": post.teaser,
            },
            "media": [item.to_dict() for item in items],
            "generator": "patreon_dl",
        }
        written.append(
            _safe_write(
                os.path.join(directory, "post.json"),
                json.dumps(payload, ensure_ascii=False, indent=2),
            )
        )
    return written


def read_post_owner(directory: str) -> str | None:
    """读取目录里 post.json 记录的作品 ID（判断目录归属）。"""
    path = os.path.join(directory, "post.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return str((payload.get("post") or {}).get("id") or "") or None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
