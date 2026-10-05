"""从 Patreon 的 JSON:API 响应中提取可下载的媒体。

真实结构（已在本机对 patreon.com 线上接口验证）：

* ``/api/posts`` 返回 JSON:API，作品在 ``data``，媒体在 ``included``。
* 作品关系里可能有 ``attachments_media`` / ``media`` / ``images`` / ``video`` / ``audio``。
* ``media`` 对象的属性：
    - ``media_type``  : image / video / audio / attachment
    - ``mimetype``    : image/jpeg、video/mp4、application/x-mpegURL ...
    - ``download_url``: 可直接下载的地址（视频/附件/原图，视权限而定）
    - ``image_urls``  : {original, url, default, default_large, thumbnail, ...}
    - ``file_name`` / ``size_bytes`` / ``metadata`` / ``display``
* 视频有两种：``stream.mux.com/<id>.m3u8?token=...``（完整版，同 token 可直接取
  ``/highest.mp4``）与 ``...- video_preview.mp4``（30 秒预览）。
* 作品属性里还有 ``post_file`` / ``image`` / ``thumbnail`` / ``embed`` / ``video_preview``。
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from .models import MediaItem, PostItem
from .util import (
    classify_extension,
    guess_extension,
    sanitize_filename,
    unique_name,
)

HLS_MIMETYPES = {"application/x-mpegurl", "application/vnd.apple.mpegurl", "application/x-mpegURL".lower()}

_MUX_RE = re.compile(
    r"https?://(?:stream\.mux\.com|image\.mux\.com)/(?P<pid>[A-Za-z0-9]+)"
    r"(?:\.m3u8|/[^?\s\"']*)?\?token=(?P<token>[A-Za-z0-9._\-]+)",
    re.IGNORECASE,
)

_IMG_TAG_RE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_LINK_TAG_RE = re.compile(r"<a\b[^>]*?\bhref\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_SOURCE_TAG_RE = re.compile(
    r"<(?:source|video|audio)\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE
)
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>)\]]+")

_MEDIA_HOSTS = (
    "patreonusercontent.com",
    "cdn.patreon.com",
    "stream.mux.com",
    "image.mux.com",
    "patreon.com/media-u/",
    "patreon-media.com",
)

# Patreon 新版富文本（content_json_string，ProseMirror 结构）里可能出现媒体引用的节点
_RICH_MEDIA_NODE_TYPES = (
    "video", "image", "file", "media", "attachment", "audio",
    "embed", "iframe", "gallery", "galleryItem", "mediaItem",
)

_MEDIA_PATH_HINTS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".avif",
    ".mp4", ".mov", ".m4v", ".webm", ".mkv", ".ts",
    ".mp3", ".m4a", ".aac", ".ogg", ".wav", ".flac",
    ".zip", ".rar", ".7z", ".psd", ".ai", ".pdf", ".clip", ".sai", ".procreate",
)

# Mux 静态渲染档位：优先完整版，其次按高度递降
_MUX_RENDITIONS = ["highest.mp4", "capped-1080p.mp4", "1080p.mp4", "720p.mp4",
                   "high.mp4", "medium.mp4", "480p.mp4", "360p.mp4", "low.mp4"]


@dataclass
class ExtractOptions:
    """影响媒体挑选方式的选项。"""

    image_quality: str = "original"      # original | large | medium
    prefer_mux_full: bool = True         # 优先 stream.mux.com 完整版视频
    include_thumbnails: bool = False     # 是否把视频封面图也当图片下载
    include_embeds: bool = False         # 是否收集 YouTube/Vimeo 等外链
    include_previews: bool = False       # 是否保留下载视频的 30 秒预览


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return default


def build_included_index(payload: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """把 ``included`` 数组索引成 {(type, id): object}。"""
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for obj in payload.get("included") or []:
        if not isinstance(obj, dict):
            continue
        oid = str(obj.get("id") or "")
        otype = str(obj.get("type") or "")
        if oid:
            index[(otype, oid)] = obj
    return index


def _looks_like_media_url(url: str) -> bool:
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return False
    low = url.lower()
    if any(host in low for host in _MEDIA_HOSTS):
        return True
    path = urlsplit(url).path.lower()
    return any(path.endswith(hint) for hint in _MEDIA_PATH_HINTS)


def _best_image_url(image_urls: dict[str, Any], quality: str) -> str | None:
    """按画质偏好从 image_urls 里挑一个地址。"""
    if not isinstance(image_urls, dict):
        return None
    chains = {
        "original": ("original", "url", "default_large", "default", "large",
                     "thumbnail_large", "thumbnail"),
        "large": ("default_large", "large", "original", "url", "default",
                  "thumbnail_large", "thumbnail"),
        "medium": ("default", "url", "default_small", "thumbnail_large",
                   "thumbnail", "default_large", "original"),
    }
    for key in chains.get(quality, chains["original"]):
        value = image_urls.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    # 最后兜底：任何看起来像图片的字符串
    for value in image_urls.values():
        if isinstance(value, str) and value.startswith("http"):
            return value
    return None


def _duration_of(attrs: dict[str, Any]) -> float | None:
    metadata = _attr(attrs, "metadata") or {}
    if isinstance(metadata, dict):
        for key in ("duration", "duration_ms", "video_duration"):
            value = metadata.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return float(value) / 1000.0 if "ms" in key else float(value)
    display = _attr(attrs, "display") or {}
    if isinstance(display, dict):
        value = display.get("duration")
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return None


def _dimensions_of(attrs: dict[str, Any]) -> tuple[int | None, int | None]:
    display = _attr(attrs, "display") or {}
    if isinstance(display, dict):
        width, height = display.get("width"), display.get("height")
        if isinstance(width, int) and isinstance(height, int):
            return width, height
    metadata = _attr(attrs, "metadata") or {}
    if isinstance(metadata, dict):
        width, height = metadata.get("width"), metadata.get("height")
        if isinstance(width, int) and isinstance(height, int):
            return width, height
    return None, None


# mux 上不是视频流的地址（字幕、故事板、封面图、文字轨），不能当成视频来源
_MUX_NON_VIDEO_HINTS = (
    "/text/", "/storyboard", "/thumbnail", "/timeline", "/captions", "/subtitles",
    ".vtt", ".srt", ".json", ".jpg", ".jpeg", ".png", ".webp",
)


def _mux_token_info(url: str) -> tuple[str, str, dict[str, Any]] | None:
    """拆出 mux 的 (播放ID, 令牌, 令牌声明)；不是视频令牌就返回 None。"""
    match = _MUX_RE.search(url or "")
    if not match:
        return None
    low = (url or "").lower()
    if any(hint in low for hint in _MUX_NON_VIDEO_HINTS):
        return None
    pid, token = match.group("pid"), match.group("token")
    claims = _jwt_claims(token)
    # 只有 aud 为 v（视频）的令牌能取视频流；封面图是 aud=t，故事板是 aud=s
    if claims.get("aud") not in (None, "v"):
        return None
    return pid, token, claims


def _mux_candidates(url: str) -> list[str]:
    """就一个 mux 地址生成不同画质的静态 MP4 候选列表。

    带 ``playback_restriction_id`` 的播放（Patreon 的受保护视频）**没有**任何
    静态 MP4，请求会得到 ``404 {"error":"MP4 does not exist"}``，此时返回空列表，
    由 :func:`_mux_hls_url` 提供的 HLS 地址来下载。
    """
    info = _mux_token_info(url)
    if not info:
        return []
    pid, token, claims = info
    if "playback_restriction_id" in claims:
        return []
    return [f"https://stream.mux.com/{pid}/{name}?token={token}" for name in _MUX_RENDITIONS]


def _mux_hls_url(url: str) -> str | None:
    """把任意 mux 视频地址规范成 HLS 主播放列表地址。"""
    info = _mux_token_info(url)
    if not info:
        return None
    pid, token, _claims = info
    return f"https://stream.mux.com/{pid}.m3u8?token={token}"


def _jwt_claims(token: str) -> dict[str, Any]:
    """解码 JWT 的 payload 部分（只读，不校验签名）。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(payload)
        data = json.loads(raw.decode("utf-8", "replace"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


# --------------------------------------------------------------- 新版富文本


@dataclass
class RichContent:
    """从 ``content_json_string`` 里解析出来的东西。"""

    media_ids: list[str] = field(default_factory=list)
    media_sections: dict[str, str] = field(default_factory=dict)   # media_id -> 所属段落标题
    section_titles: list[str] = field(default_factory=list)        # 按出现顺序去重
    inline_urls: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    text: str = ""
    text_parts: list[str] = field(default_factory=list)
    node_counts: dict[str, int] = field(default_factory=dict)


def _node_text(node: Any) -> str:
    """取一个富文本节点里的纯文本。"""
    parts: list[str] = []

    def walk(current: Any, depth: int = 0) -> None:
        if depth > 24:
            return
        if isinstance(current, list):
            for child in current:
                walk(child, depth + 1)
            return
        if not isinstance(current, dict):
            return
        if current.get("type") == "text":
            parts.append(str(current.get("text") or ""))
        for child in current.get("content") or []:
            walk(child, depth + 1)

    walk(node)
    return "".join(parts).strip()


def _section_label(heading_stack: dict[int, str], last_label: str) -> str:
    """拼出这一段的小标题：上级 heading 路径 + 最近的一段文字。"""
    parts: list[str] = []
    for level in sorted(heading_stack):
        text = (heading_stack.get(level) or "").strip()
        if text and text != last_label and text not in parts:
            parts.append(text)
    last_label = (last_label or "").strip()
    if last_label and last_label not in parts:
        parts.append(last_label)
    return " - ".join(parts)


def parse_rich_content(raw: str | None) -> RichContent:
    """解析 Patreon 新版正文（``content_json_string``）。

    新版编辑器把正文存成 ProseMirror 文档，媒体以
    ``{"type":"video","attrs":{"media_id":"739888749"}}`` 这种形式内嵌在正文里，
    **不会**出现在 ``relationships`` 中——只能靠解析这里才能拿到。

    典型例子：一篇「索引贴」里嵌了 21 个视频，每个视频前面用
    ``heading`` 或 ``blockquote`` 写着它的标题。这里会顺便按标题把视频分段，
    方便下载时一个部分一个子目录。
    """
    result = RichContent()
    if not raw:
        return result
    try:
        doc = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return result

    def collect_media(node: Any, section: str, depth: int = 0) -> None:
        """递归收集媒体节点，并打上所属段落标题。"""
        if depth > 24:
            return
        if isinstance(node, list):
            for child in node:
                collect_media(child, section, depth + 1)
            return
        if not isinstance(node, dict):
            return
        node_type = str(node.get("type") or "")
        attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}

        if node_type in _RICH_MEDIA_NODE_TYPES or "media_id" in attrs:
            media_id = attrs.get("media_id") or attrs.get("mediaId") or attrs.get("id")
            if media_id is not None and str(media_id).strip():
                value = str(media_id).strip()
                if value not in result.media_ids:
                    result.media_ids.append(value)
                if section:
                    result.media_sections.setdefault(value, section)
                if section and section not in result.section_titles:
                    result.section_titles.append(section)
            for key in ("src", "url", "href", "download_url"):
                value = attrs.get(key)
                if isinstance(value, str) and value.startswith("http"):
                    result.inline_urls.append(value)
                    break

        for child in node.get("content") or []:
            collect_media(child, section, depth + 1)

    def walk_meta(node: Any, depth: int = 0) -> None:
        """收集纯文本、超链接与节点统计。"""
        if depth > 24:
            return
        if isinstance(node, list):
            for child in node:
                walk_meta(child, depth + 1)
            return
        if not isinstance(node, dict):
            return
        node_type = str(node.get("type") or "")
        if node_type:
            result.node_counts[node_type] = result.node_counts.get(node_type, 0) + 1
        if node_type == "text":
            if node.get("text"):
                result.text_parts.append(str(node["text"]))
            for mark in node.get("marks") or []:
                if isinstance(mark, dict) and mark.get("type") == "link":
                    href = str((mark.get("attrs") or {}).get("href") or "").strip()
                    if href:
                        result.links.append(href)
        for child in node.get("content") or []:
            walk_meta(child, depth + 1)

    # 顶层按顺序扫描，维护 heading 层级与「最近一段文字」
    children = doc.get("content") if isinstance(doc, dict) else None
    if isinstance(children, list):
        heading_stack: dict[int, str] = {}
        last_label = ""
        for child in children:
            if not isinstance(child, dict):
                continue
            node_type = str(child.get("type") or "")
            if node_type == "heading":
                attrs = child.get("attrs") if isinstance(child.get("attrs"), dict) else {}
                try:
                    level = int(attrs.get("level") or 1)
                except (TypeError, ValueError):
                    level = 1
                text = _node_text(child)
                heading_stack[level] = text
                for deeper in [k for k in heading_stack if k > level]:
                    del heading_stack[deeper]
                if text:
                    last_label = text
                continue
            if node_type in ("paragraph", "blockquote"):
                text = _node_text(child)
                if text:
                    last_label = text
            collect_media(child, _section_label(heading_stack, last_label))
    else:
        collect_media(doc, "")

    result.text_parts = []          # 收集阶段先装满，最后拼成纯文本
    walk_meta(doc)
    result.text = "\n".join(t for t in result.text_parts if t.strip()).strip()
    result.text_parts = []
    return result


def _mux_playback(url: str) -> tuple[str, str] | None:
    match = _MUX_RE.search(url or "")
    if not match:
        return None
    return match.group("pid"), match.group("token")


def _kind_of(attrs: dict[str, Any]) -> str:
    mimetype = str(_attr(attrs, "mimetype") or "").lower()
    media_type = str(_attr(attrs, "media_type") or "").lower()
    if mimetype.startswith("image/"):
        return "image"
    if mimetype.startswith("video/") or mimetype in HLS_MIMETYPES:
        return "video"
    if mimetype.startswith("audio/"):
        return "audio"
    if media_type in ("image", "video", "audio", "attachment"):
        return media_type
    if _attr(attrs, "image_urls"):
        return "image"
    if _attr(attrs, "download_url"):
        return "attachment"
    return "other"


def _base_name(name: str | None) -> str:
    return (name or "").strip()


def _is_preview_name(name: str | None) -> bool:
    low = _base_name(name).lower()
    return "preview" in low


def _preview_playback_id(attrs: dict[str, Any]) -> str | None:
    """判断 ``video_preview`` 是不是一个**独立的**预告资产，是则返回它的播放 ID。

    只有当预告时长明显短于完整时长时才认（``duration < full*0.95``）。
    万一两者相等（预告其实就是整片），就不能把它当预告丢掉。
    """
    preview = _attr(attrs, "video_preview")
    if not isinstance(preview, dict):
        return None
    playback = _mux_playback(str(preview.get("url") or ""))
    if not playback:
        return None

    def as_float(value: Any) -> float | None:
        return float(value) if isinstance(value, (int, float)) and value > 0 else None

    short = as_float(preview.get("duration"))
    full = as_float(preview.get("full_content_duration"))
    if short is not None and full is not None and short >= full * 0.95:
        return None            # 预告就是整片，别当预告
    return playback[0]


class _PostExtractor:
    """针对单个作品的提取器（维护去重与命名状态）。"""

    def __init__(self, post: PostItem, options: ExtractOptions):
        self.post = post
        self.options = options
        self.items: list[MediaItem] = []
        self._seen_urls: set[str] = set()
        self._used_names: set[str] = set()
        self._mux_seen: set[str] = set()
        self._has_mux_full = False
        # 「预告」用的 mux 播放 ID。Patreon 有时会给预告单独建一个 mux 资产
        # （例如 46 秒的剪辑），而 relationship 里那个 media 的 download_url
        # 恰恰指向它 —— 必须靠播放 ID 区分，否则会把预告当成完整版。
        self.preview_playback_ids: set[str] = set()

    # --------------------------------------------------------------- 工具
    def _index_of(self) -> int:
        return sum(1 for i in self.items if i.kind in ("image", "video")) + 1

    def _add(self, item: MediaItem) -> None:
        if not item.url or not item.url.startswith("http"):
            return
        fingerprint = item.key
        if fingerprint in self._seen_urls:
            return
        self._seen_urls.add(fingerprint)
        self.items.append(item)

    def add_url(self, url: str, kind: str = "other", source: str = "scan", **kwargs: Any) -> None:
        """外部（兜底扫描）添加一条媒体。"""
        if not isinstance(url, str) or not url.startswith("http"):
            return
        self._add(self._new(url, kind, source=source, **kwargs))

    # ------------------------------------------------------------ 命名收尾
    def finalize(self) -> None:
        """给没有真实文件名的条目按作品标题命名，并保证同目录内唯一。"""
        video_total = sum(1 for i in self.items if i.kind == "video")
        counters: dict[str, int] = {}
        for item in self.items:
            counters[item.kind] = counters.get(item.kind, 0) + 1
            if not item.filename:
                item.filename = self._derive_name(item, counters[item.kind], video_total)
            stem, ext = os.path.splitext(item.filename)
            item.filename = unique_name(
                sanitize_filename(stem, "file", max_length=120) + ext, self._used_names
            )

    def _derive_name(self, item: MediaItem, index: int, video_total: int) -> str:
        title = self.post.safe_title
        ext = guess_extension(item.mimetype, item.url, default="")
        if item.kind == "video":
            ext = ext or "mp4"
            if video_total > 1:
                return f"{title} - {index:02d}.{ext}"
            return f"{title}.{ext}"
        if item.kind == "image":
            return f"{title} - {index:02d}.{ext or 'jpg'}"
        if item.kind == "audio":
            return f"{title} - {index:02d}.{ext or 'mp3'}"
        if item.kind == "embed":
            return f"{title} (embed)"
        return f"{title} - {index:02d}.{ext or 'bin'}"

    # ------------------------------------------------------- mux 完整版推导
    def derive_mux_video(self, url: str, duration: float | None = None) -> None:
        """任何 mux 视频地址都能推出完整版。

        优先静态 MP4（``highest.mp4`` 等，单个文件最快）；如果这个播放带
        ``playback_restriction_id``（Patreon 的受保护视频），mux 不提供静态
        MP4，就直接用 ``.m3u8``，交给下载引擎的 HLS 分片下载。
        """
        if not self.options.prefer_mux_full or self._has_mux_full:
            return
        # 预告那个资产的播放 ID 绝不能当成完整版
        playback = _mux_playback(url)
        if playback and playback[0] in self.preview_playback_ids:
            return
        hls = _mux_hls_url(url)
        candidates = _mux_candidates(url)
        if not candidates and not hls:
            return
        if candidates:
            primary, fallbacks = candidates[0], list(candidates[1:])
            if hls and hls not in fallbacks:
                fallbacks.append(hls)      # 静态 MP4 全都取不到时再走 HLS
        else:
            primary, fallbacks = hls, []
        item = self._new(
            primary, "video", mimetype="video/mp4", source="mux-derived",
            duration=duration, is_preview=False,
        )
        item.extra["fallbacks"] = fallbacks
        item.extra["derived"] = True
        item.extra["hls"] = hls or ""
        self._add(item)
        self._has_mux_full = True

    def derive_mux_from_tree(self, node: Any, depth: int = 0) -> None:
        if self._has_mux_full or depth > 10:
            return
        if isinstance(node, dict):
            for value in node.values():
                self.derive_mux_from_tree(value, depth + 1)
                if self._has_mux_full:
                    return
        elif isinstance(node, list):
            for value in node:
                self.derive_mux_from_tree(value, depth + 1)
                if self._has_mux_full:
                    return
        elif isinstance(node, str) and "mux.com" in node:
            self.derive_mux_video(node)

    def _new(self, url: str, kind: str, **kwargs: Any) -> MediaItem:
        return MediaItem(
            url=url,
            kind=kind,
            post_id=self.post.id,
            referer=self.post.url,
            **kwargs,
        )

    # ------------------------------------------------- 各个来源的收集逻辑
    def collect_media_object(self, attrs: dict[str, Any], media_id: str | None) -> None:
        if not isinstance(attrs, dict):
            return
        kind = _kind_of(attrs)
        mimetype = _attr(attrs, "mimetype")
        download_url = _attr(attrs, "download_url")
        image_urls = _attr(attrs, "image_urls") or {}
        file_name = _attr(attrs, "file_name")
        size = _attr(attrs, "size_bytes")
        width, height = _dimensions_of(attrs)
        duration = _duration_of(attrs)
        media_type = str(_attr(attrs, "media_type") or "").lower()

        # 带 mux 播放地址的媒体通常就是完整版；但如果这个地址属于「预告」
        # 那个独立资产，就不能算 —— 否则 post_file 里的真完整版会被跳过。
        if isinstance(download_url, str) and "stream.mux.com" in download_url:
            playback = _mux_playback(download_url)
            is_preview_asset = bool(
                playback and playback[0] in self.preview_playback_ids
            )
            if playback:
                self._mux_seen.add(playback[0])
            if not is_preview_asset:
                self._has_mux_full = True

        if kind == "video":
            is_hls = str(mimetype or "").lower() in HLS_MIMETYPES
            if is_hls:
                url = download_url if isinstance(download_url, str) else None
                if not url:
                    # 只有 m3u8 时，尝试把它换成 mux 的完整版 mp4
                    for candidate in (
                        _attr(attrs, "url"),
                        _attr(_attr(attrs, "metadata") or {}, "url"),
                    ):
                        if isinstance(candidate, str) and candidate.startswith("http"):
                            url = candidate
                            break
                if not url and isinstance(image_urls, dict):
                    url = image_urls.get("url")
                if not url:
                    return
                # mux 播放：优先静态 MP4，受保护播放则只有 HLS 可用
                candidates = _mux_candidates(url) if self.options.prefer_mux_full else []
                hls = _mux_hls_url(url) if self.options.prefer_mux_full else None
                playback = _mux_playback(url)
                # 这个媒体是不是「预告」那个资产？
                is_preview_asset = bool(
                    playback and playback[0] in self.preview_playback_ids
                )
                if candidates:
                    primary = candidates[0]
                    fallbacks = list(candidates[1:])
                    if hls and hls not in fallbacks:
                        fallbacks.append(hls)
                elif hls:
                    primary, fallbacks = hls, []
                else:
                    primary, fallbacks = url, []

                is_preview = is_preview_asset or _is_preview_name(file_name)
                if is_preview and not self.options.include_previews:
                    # 不下预告；也**不能**让它占用「已有完整版」的名额，
                    # 否则 post_file 里的真完整版会被跳过
                    return

                item = self._new(
                    primary, "video", mimetype="video/mp4" if (candidates or hls) else mimetype,
                    media_id=media_id, size=None, width=width, height=height,
                    duration=duration, source="media", is_preview=is_preview,
                )
                if file_name:
                    item.filename = file_name
                # size_bytes 是源片大小，封装后的文件不会完全一致，
                # 所以只用于界面显示，不能拿来做「已下载」的大小校验
                if size:
                    item.extra["reported_size"] = int(size)
                if fallbacks:
                    item.extra["fallbacks"] = fallbacks
                if hls:
                    item.extra["hls"] = hls
                if not is_preview_asset:
                    self._has_mux_full = True
                self._add(item)
                return

            if not isinstance(download_url, str) or not download_url.startswith("http"):
                download_url = image_urls.get("url") if isinstance(image_urls, dict) else None
            if not isinstance(download_url, str) or not download_url.startswith("http"):
                return
            preview = _is_preview_name(file_name) or media_type == "video" and self._has_mux_full
            item = self._new(
                download_url, "video", mimetype=mimetype, media_id=media_id, size=size,
                width=width, height=height, duration=duration, source="media",
                is_preview=preview,
            )
            if file_name:
                item.filename = file_name
            if preview and not self.options.include_previews:
                return
            self._add(item)
            return

        if kind == "image":
            url = None
            if isinstance(download_url, str) and download_url.startswith("http"):
                url = download_url
            if not url:
                url = _best_image_url(image_urls, self.options.image_quality)
            if not url:
                return
            item = self._new(
                url, "image", mimetype=mimetype or "image/jpeg", media_id=media_id,
                size=size, width=width, height=height, source="media",
            )
            if file_name:
                item.filename = file_name
            self._add(item)
            return

        # 音频 / 附件 / 其它
        if not isinstance(download_url, str) or not download_url.startswith("http"):
            download_url = image_urls.get("url") if isinstance(image_urls, dict) else None
        if not isinstance(download_url, str) or not download_url.startswith("http"):
            return
        if kind == "other":
            ext = guess_extension(mimetype, download_url, default="")
            kind = classify_extension(ext) if ext else "attachment"
        item = self._new(
            download_url, kind, mimetype=mimetype, media_id=media_id, size=size,
            duration=duration, source="media",
        )
        if file_name:
            item.filename = file_name
        if item.kind == "video" and item.is_preview and not self.options.include_previews:
            return
        self._add(item)

    def collect_media_collection(self, attrs: dict[str, Any], owner_kind: str) -> None:
        """处理以字典集合形式出现的媒体字段（如某些版本的 ``media``）。"""
        if isinstance(attrs, dict):
            for key, value in attrs.items():
                if isinstance(value, str) and _looks_like_media_url(value):
                    kind = classify_extension(guess_extension(None, value, ""))
                    self._add(self._new(value, kind if kind != "other" else owner_kind,
                                        source="collection"))

    def collect_content_html(self, content: str | None) -> None:
        if not content:
            return
        text = html.unescape(str(content))
        found: list[str] = []
        for regex in (_IMG_TAG_RE, _SOURCE_TAG_RE):
            found.extend(regex.findall(text))
        for href in _LINK_TAG_RE.findall(text):
            if _looks_like_media_url(href):
                found.append(href)
        for url in found:
            url = html.unescape(url.strip())
            if not url.startswith("http"):
                continue
            ext = guess_extension(None, url, default="")
            kind = classify_extension(ext) or "image"
            self._add(self._new(url, kind, source="content"))

    def collect_thumbnails(self, attrs: dict[str, Any]) -> None:
        """作品封面 / 首图（仅在开启或没有其它媒体时使用）。"""
        candidates: list[str] = []
        image = _attr(attrs, "image")
        if isinstance(image, dict):
            for key in ("url", "large_url", "thumb_url"):
                value = image.get(key)
                if isinstance(value, str):
                    candidates.append(value)
        thumbnail = _attr(attrs, "thumbnail")
        if isinstance(thumbnail, dict):
            for key in ("url", "original", "default_large", "default"):
                value = thumbnail.get(key)
                if isinstance(value, str):
                    candidates.append(value)
        if not candidates:
            return
        url = candidates[0]
        if not url.startswith("http"):
            return
        # image.mux.com/<id>/thumbnail.jpg 也携带完整版视频的 token
        if "mux.com" in url:
            self.derive_mux_video(url)
        width, height = _dimensions_of({"display": image} if isinstance(image, dict) else {})
        item = self._new(url, "image", source="thumbnail", media_id=None,
                         width=width, height=height)
        self._add(item)

    def collect_post_file(self, attrs: dict[str, Any]) -> None:
        """``post_file``：附件类作品才有真正的下载地址。

        对视频作品，``post_file`` 里可能是完整的播放地址（新版 Patreon 把
        视频的 mux 播放信息放在这里，而不是 ``media.download_url``），
        这时直接用来推导完整版视频。
        """
        post_file = _attr(attrs, "post_file")
        if not isinstance(post_file, dict):
            return
        url = post_file.get("url")
        if not isinstance(url, str) or not url.startswith("http"):
            return
        name = post_file.get("name") or post_file.get("file_name")
        if not name:
            # 没有文件名 -> 不是附件；如果它是 mux 播放地址，就是本篇的完整视频
            if "mux.com" in url:
                duration = post_file.get("full_content_duration") or post_file.get("duration")
                self.derive_mux_video(url, duration if isinstance(duration, (int, float)) else None)
            return
        ext = guess_extension(None, name, default="")
        kind = classify_extension(ext) or "attachment"
        item = self._new(url, kind, source="post_file", size=post_file.get("size")
                         or post_file.get("size_bytes"))
        item.filename = str(name)
        self._add(item)

    def collect_video_preview(self, attrs: dict[str, Any]) -> None:
        """``video_preview``：mux 的 30 秒预览，同时提供完整版线索。"""
        preview = _attr(attrs, "video_preview")
        if not isinstance(preview, dict):
            return
        url = preview.get("url")
        if not isinstance(url, str) or not url.startswith("http"):
            return
        playback = _mux_playback(url)
        if playback:
            self._mux_seen.add(playback[0])
        self.derive_mux_video(url, preview.get("full_content_duration"))
        if self.options.include_previews:
            item = self._new(url, "video", mimetype="video/mp4", source="mux-preview",
                             duration=preview.get("duration"), is_preview=True)
            self._add(item)

    def collect_embed(self, attrs: dict[str, Any]) -> None:
        embed = _attr(attrs, "embed")
        if not isinstance(embed, dict):
            return
        url = embed.get("url")
        if not isinstance(url, str) or not url.startswith("http"):
            return
        item = self._new(url, "embed", source="embed")
        provider = embed.get("provider") or ""
        subject = embed.get("subject") or ""
        item.extra["provider"] = str(provider)
        item.extra["subject"] = str(subject)
        item.filename = f"{provider} - {subject}".strip(" -") or "embed"
        self._add(item)

    def deep_scan(self, node: Any, depth: int = 0) -> None:
        """兜底扫描：Patreon 字段偶尔变动，这里抓漏网的媒体地址。"""
        if depth > 12:
            return
        if isinstance(node, dict):
            for value in node.values():
                self.deep_scan(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                self.deep_scan(value, depth + 1)
        elif isinstance(node, str):
            if _looks_like_media_url(node):
                ext = guess_extension(None, node, default="")
                kind = classify_extension(ext) or "attachment"
                self._add(self._new(node, kind, source="scan"))


def extract_post(
    post_obj: dict[str, Any],
    included: dict[tuple[str, str], dict[str, Any]],
    options: ExtractOptions | None = None,
    campaign_id: str = "",
    creator: str = "",
    media_resolver: Callable[[str], dict[str, Any] | None] | None = None,
) -> PostItem:
    """把一条 ``/api/posts`` 记录转成 :class:`PostItem`（含媒体清单）。

    ``media_resolver``：正文（``content_json_string``）里内嵌的媒体只给了
    ``media_id``，需要调用方按 ID 去取（``GET /api/media/{id}``）。
    传入这个回调后，程序会自动把它们补进媒体清单。
    """
    options = options or ExtractOptions()
    attrs = dict(_attr(post_obj, "attributes") or {})
    post_id = str(post_obj.get("id") or "")

    post = PostItem(
        id=post_id,
        title=str(attrs.get("title") or "").strip(),
        published_at=str(attrs.get("published_at") or ""),
        edited_at=str(attrs.get("edited_at") or ""),
        post_type=str(attrs.get("post_type") or ""),
        url=str(attrs.get("url") or ""),
        campaign_id=str(campaign_id or ""),
        creator=creator,
        is_paid=bool(attrs.get("is_paid")),
        can_view=bool(attrs.get("current_user_can_view", True)),
        min_cents=int(attrs.get("min_cents_pledged_to_view") or 0),
        teaser=str(attrs.get("teaser_text") or ""),
        summary=str(attrs.get("content") or ""),
        raw={"attributes": attrs, "id": post_id},
    )

    extractor = _PostExtractor(post, options)
    relationships = _attr(post_obj, "relationships") or {}

    # 0) 先认出「预告」用的是哪个 mux 资产。
    #    有的作品预告和完整版是两个不同的 mux 资产，而 relationship 里那个
    #    media 的 download_url 可能指向预告 —— 不先分辨就会把预告当完整版。
    preview_playback_id = _preview_playback_id(attrs)
    if preview_playback_id:
        extractor.preview_playback_ids.add(preview_playback_id)

    # 1) 关系里引用的 media 对象
    for rel_name in ("attachments_media", "media", "images", "video", "audio"):
        rel = relationships.get(rel_name) or {}
        data = rel.get("data")
        if not data:
            continue
        refs = data if isinstance(data, list) else [data]
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            obj = included.get((str(ref.get("type")), str(ref.get("id"))))
            if obj is None:
                obj = included.get(("media", str(ref.get("id"))))
            if obj is None:
                continue
            extractor.collect_media_object(_attr(obj, "attributes") or {}, str(ref.get("id")))

    # 2) 作品自身的媒体字段
    extractor.collect_post_file(attrs)
    extractor.collect_video_preview(attrs)

    # 2b) 任意 mux 线索（含 image.mux.com 封面）都能推出完整版视频
    if options.prefer_mux_full and not extractor._has_mux_full:
        extractor.derive_mux_from_tree(attrs)

    has_rich_media = any(i.kind in ("image", "video", "audio", "attachment") for i in extractor.items)
    if options.include_thumbnails or not has_rich_media:
        extractor.collect_thumbnails(attrs)

    # 3) 正文里的内联媒体
    extractor.collect_content_html(attrs.get("content"))

    # 3b) 新版富文本正文（content_json_string）
    #     这里的媒体只给了 media_id，不在 relationships 里，必须单独取
    rich = parse_rich_content(attrs.get("content_json_string"))
    if rich.text and not post.summary:
        post.summary = rich.text                      # 老字段为空时用新正文
    for url in rich.inline_urls:
        if _looks_like_media_url(url):
            ext = guess_extension(None, url, default="")
            kind = classify_extension(ext) or "image"
            extractor.add_url(url, kind, source="content-json")

    # 4) 外链视频
    if options.include_embeds:
        extractor.collect_embed(attrs)

    # 5) 兜底扫描正文中出现的裸链接
    content_text = " ".join(filter(None, [attrs.get("content") or "", rich.text or ""]))
    if content_text:
        for raw_url in _URL_IN_TEXT_RE.findall(html.unescape(str(content_text))):
            if _looks_like_media_url(raw_url):
                ext = guess_extension(None, raw_url, default="")
                kind = classify_extension(ext) or "attachment"
                extractor.add_url(raw_url, kind, source="scan")

    # 6) 取回富文本里内嵌的媒体对象
    if media_resolver is not None:
        for media_id in rich.media_ids:
            try:
                obj = media_resolver(media_id)
            except Exception:  # noqa: BLE001 - 单个取不到不应影响整篇
                obj = None
            if isinstance(obj, dict):
                before = len(extractor.items)
                extractor.collect_media_object(_attr(obj, "attributes") or {}, media_id)
                section = rich.media_sections.get(media_id, "")
                if section:
                    for created in extractor.items[before:]:
                        created.section = section
        post.inline_media_total = len(rich.media_ids)

    post.section_titles = list(rich.section_titles)
    extractor.finalize()
    post.media = extractor.items
    return post


def extract_posts(
    payload: dict[str, Any],
    options: ExtractOptions | None = None,
    campaign_id: str = "",
    creator: str = "",
    media_resolver: Callable[[str], dict[str, Any] | None] | None = None,
) -> list[PostItem]:
    """把一页 ``/api/posts`` 响应转成作品列表。"""
    included = build_included_index(payload)
    results: list[PostItem] = []
    for post_obj in payload.get("data") or []:
        if not isinstance(post_obj, dict):
            continue
        try:
            results.append(
                extract_post(post_obj, included, options, campaign_id, creator,
                             media_resolver=media_resolver)
            )
        except Exception:  # noqa: BLE001 - 单条作品出错不应中断整页
            continue
    return results


def filter_by_kind(items: Iterable[MediaItem], config: Any) -> list[MediaItem]:
    """按用户勾选的内容类型过滤媒体。"""
    result: list[MediaItem] = []
    for item in items:
        if item.kind == "image" and not config.download_images:
            continue
        if item.kind == "video":
            if item.is_preview:
                if not config.download_previews:
                    continue
            elif not config.download_videos:
                continue
        if item.kind == "audio" and not config.download_audio:
            continue
        if item.kind in ("attachment", "other") and not config.download_attachments:
            continue
        if item.kind == "embed" and not config.use_yt_dlp:
            continue
        result.append(item)
    return result
