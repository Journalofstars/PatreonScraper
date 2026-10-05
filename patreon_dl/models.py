"""数据模型：创作者、作品、媒体条目。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .util import human_duration, human_size, stable_url_key

MEDIA_KINDS = ("image", "video", "audio", "attachment", "embed", "other")

KIND_LABELS = {
    "image": "图片",
    "video": "视频",
    "audio": "音频",
    "attachment": "附件",
    "embed": "外链视频",
    "other": "其他",
}


@dataclass
class MediaItem:
    """一条可下载的媒体。"""

    url: str
    kind: str = "other"
    filename: str = ""
    mimetype: str | None = None
    size: int | None = None
    media_id: str | None = None
    post_id: str = ""
    source: str = "media"        # media / post_file / content / embed / scan
    is_preview: bool = False
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    referer: str = ""
    section: str = ""                  # 所属段落标题（新版富文本作品里的「第几部分」）
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """跨运行稳定的唯一标识（忽略签名参数）。"""
        if self.media_id:
            return f"{self.post_id}:m{self.media_id}"
        return f"{self.post_id}:u{stable_url_key(self.url)}"

    @property
    def kind_label(self) -> str:
        label = KIND_LABELS.get(self.kind, self.kind)
        return f"{label}(预览)" if self.is_preview else label

    @property
    def display_size(self) -> int | None:
        """界面显示用的大小：优先真实大小，HLS 视频退回接口报告值。"""
        if self.size:
            return self.size
        reported = self.extra.get("reported_size") if self.extra else None
        return int(reported) if reported else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "kind": self.kind,
            "filename": self.filename,
            "mimetype": self.mimetype,
            "size": self.size,
            "media_id": self.media_id,
            "source": self.source,
            "is_preview": self.is_preview,
            "duration": self.duration,
            "width": self.width,
            "height": self.height,
        }

    def describe(self) -> str:
        bits = [self.kind_label]
        if self.size:
            bits.append(human_size(self.size))
        if self.duration:
            bits.append(human_duration(self.duration))
        if self.width and self.height:
            bits.append(f"{self.width}x{self.height}")
        return " · ".join(bits)


@dataclass
class PostItem:
    """一篇作品。"""

    id: str
    title: str = ""
    site: str = "patreon"               # 内容来源站点：patreon / joi
    published_at: str = ""
    edited_at: str = ""
    post_type: str = ""
    url: str = ""
    campaign_id: str = ""
    creator: str = ""
    is_paid: bool = False
    can_view: bool = True
    min_cents: int = 0
    teaser: str = ""
    summary: str = ""
    media: list[MediaItem] = field(default_factory=list)
    inline_media_total: int = 0        # 正文富文本里内嵌的媒体个数
    section_titles: list[str] = field(default_factory=list)   # 正文里划分出的段落
    raw: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------- 派生属性
    @property
    def published_date(self) -> datetime:
        return parse_datetime(self.published_at)

    @property
    def date_text(self) -> str:
        dt = self.published_date
        return dt.strftime("%Y-%m-%d %H:%M") if dt else "(未知时间)"

    @property
    def date_key(self) -> str:
        """命名模板里的 ``{date}``。

        没有发布日期时返回**空串**而不是 ``0000-00-00``：某些站点（例如
        JOI Database）列表页不提供日期，占位符会污染文件夹名，
        而空串会被 ``naming._render()`` 连同多余的分隔符一起清掉。
        """
        dt = self.published_date
        return dt.strftime("%Y-%m-%d") if dt else ""

    @property
    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for item in self.media:
            result[item.kind] = result.get(item.kind, 0) + 1
        return result

    @property
    def media_count(self) -> int:
        return len(self.media)

    @property
    def distinct_sections(self) -> list[str]:
        """正文里实际划分出的段落（按媒体出现顺序去重）。"""
        titles: list[str] = []
        for item in self.media:
            if item.section and item.section not in titles:
                titles.append(item.section)
        return titles or list(self.section_titles)

    def count_text(self) -> str:
        counts = self.counts
        if not counts:
            return "无"
        parts = []
        for kind in MEDIA_KINDS:
            if counts.get(kind):
                parts.append(f"{KIND_LABELS.get(kind, kind)}{counts[kind]}")
        return " / ".join(parts)

    @property
    def safe_title(self) -> str:
        title = (self.title or "").strip()
        title = re.sub(r"\s+", " ", title)
        return title or f"post-{self.id}"

    def total_size(self) -> int:
        """作品体积合计；HLS 视频没有精确大小，用接口报告值估算。"""
        return sum(
            int(m.size or (m.extra.get("reported_size") if m.extra else 0) or 0)
            for m in self.media
        )


@dataclass
class Campaign:
    """创作者主页（campaign）。"""

    id: str
    name: str = ""
    vanity: str = ""
    url: str = ""
    summary: str = ""
    patron_count: int = 0
    image_url: str = ""
    created_at: str = ""

    @property
    def display_name(self) -> str:
        return self.name or self.vanity or f"campaign-{self.id}"


@dataclass
class Collection:
    """创作者整理的「合集」（把作品分组，比如「| Femdom |」）。

    接口是 ``GET /api/collection/{id}``（**单数**，不是 collections）；
    列出一个创作者的全部合集用 ``GET /api/collection?filter[campaign_id]=…``。
    ``post_ids`` 保持创作者自定义的排序（``post_sort_type == "custom"``）。
    """

    id: str
    title: str = ""
    description: str = ""
    campaign_id: str = ""
    post_count: int = 0
    post_ids: list[str] = field(default_factory=list)
    thumbnail: str = ""
    created_at: str = ""
    sort_type: str = ""

    @property
    def display_name(self) -> str:
        return (self.title or "").strip() or f"合集 {self.id}"

    @property
    def menu_label(self) -> str:
        return f"{self.display_name}（{self.post_count} 篇）"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "campaign_id": self.campaign_id,
            "post_count": self.post_count,
            "post_ids": list(self.post_ids),
            "created_at": self.created_at,
            "sort_type": self.sort_type,
        }


def parse_datetime(value: str | None) -> datetime | None:
    """解析 Patreon 返回的 ISO 时间。"""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()
