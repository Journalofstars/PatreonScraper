"""按模板生成下载目录与文件名。"""

from __future__ import annotations

import os
import re
from typing import Any

from .config import AppConfig
from .models import Campaign, MediaItem, PostItem
from .util import sanitize_dirname, sanitize_filename

_WIN_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _render(template: str, values: dict[str, Any], fallback: str) -> str:
    """渲染模板；模板出错时退回到 fallback。"""
    template = (template or "").strip() or fallback
    try:
        rendered = template.format_map(_SafeDict(values))
    except (KeyError, IndexError, ValueError, AttributeError):
        rendered = fallback.format_map(_SafeDict(values))
    rendered = rendered.strip()
    return rendered or fallback


class _SafeDict(dict):
    """缺字段时留下空串而不是抛 KeyError。"""

    def __missing__(self, key: str) -> str:  # noqa: D105
        return ""


def _post_values(campaign: Campaign, post: PostItem) -> dict[str, Any]:
    date = post.published_date
    return {
        "creator": campaign.display_name,
        "vanity": campaign.vanity or campaign.display_name,
        "campaign": campaign.display_name,
        "campaign_id": campaign.id,
        "post_id": post.id,
        "title": post.safe_title,
        "date": post.date_key,
        "datetime": date.strftime("%Y-%m-%d_%H%M") if date else "0000-00-00_0000",
        "year": date.strftime("%Y") if date else "0000",
        "month": date.strftime("%m") if date else "00",
        "day": date.strftime("%d") if date else "00",
        "post_type": post.post_type or "post",
        "type": post.post_type or "post",
    }


def creator_directory(config: AppConfig, campaign: Campaign) -> str:
    name = _render(
        config.folder_template,
        {
            "creator": campaign.display_name,
            "vanity": campaign.vanity or campaign.display_name,
            "campaign_id": campaign.id,
        },
        campaign.display_name,
    )
    return os.path.join(config.output_dir, sanitize_dirname(name, f"campaign-{campaign.id}"))


def post_directory(config: AppConfig, campaign: Campaign, post: PostItem,
                   existing_owner: str | None = None) -> str:
    """返回该作品的下载目录。

    ``existing_owner``：该目录若已存在且属于另一篇作品，会自动追加作品 ID 以免混在一起。
    """
    base = creator_directory(config, campaign)
    if not config.group_by_post:
        return base
    name = _render(config.name_template, _post_values(campaign, post), f"{post.date_key}_{post.safe_title}")
    name = sanitize_dirname(name, f"post-{post.id}")
    if existing_owner and existing_owner != post.id:
        name = f"{name} [{post.id}]"
    return os.path.join(base, name)


def section_directory(config: AppConfig, campaign: Campaign, post: PostItem,
                      title: str, index: int) -> str:
    """作品内部「一个部分」的子目录名（帖子含多个部分时用）。"""
    values = _post_values(campaign, post)
    values.update(
        {
            "index": index,
            "n": index,
            "title": title or f"part-{index:02d}",
            "name": title or f"part-{index:02d}",
            "section": title or f"part-{index:02d}",
        }
    )
    rendered = _render(
        config.section_template, values, "{index:02d}_{title}"
    )
    return sanitize_dirname(rendered, f"part-{index:02d}")


def media_filename(config: AppConfig, campaign: Campaign, post: PostItem,
                   item: MediaItem, index: int) -> str:
    """渲染单个媒体的文件名（含扩展名）。"""
    stem, ext = os.path.splitext(item.filename or "")
    values = _post_values(campaign, post)
    values.update(
        {
            "name": stem or item.kind,
            "stem": stem or item.kind,
            "ext": ext.lstrip("."),
            "kind": item.kind,
            "index": index,
            "n": index,
            "media_id": item.media_id or "",
        }
    )
    rendered = _render(config.file_template, values, "{index:02d}_{name}")
    if not rendered:
        rendered = f"{index:02d}_{stem or item.kind}"
    stem2, ext2 = os.path.splitext(rendered)
    if not ext2 and ext:
        rendered = rendered + ext
    return sanitize_filename(rendered, f"{item.kind}-{index}")


def unique_path(directory: str, filename: str) -> str:
    """目标文件已存在且不是我们要写的那份时，换一个名字。"""
    path = os.path.join(directory, filename)
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(filename)
    counter = 2
    while True:
        candidate = os.path.join(directory, f"{stem} ({counter}){ext}")
        if not os.path.exists(candidate):
            return candidate
        counter += 1
