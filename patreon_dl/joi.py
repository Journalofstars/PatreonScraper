"""the-joi-database.com 适配器。

站点结构（2026-10 实测，服务端渲染的 PHP 页面，无前端框架）：

* **作者页** ``/profile/<profile_id>`` 一次列出该创作者**全部**视频
  （实测 121 个，四种排序下数量一致，没有分页控件）。每条是一个自定义元素::

      <div class="... video-thumbnail-block" id="video-block-17796">
        <asis-video-thumbnail video-title="[VOICED] Bratty Bunny …"
                              duration="11:43"
                              video-id="fa211fa917ee1e495850e09d"
                              explicit="" is-patreon-exclusive=""
                              thumbnail="https://cdn-s…/thumbnail_…webp?ud=…">
        </asis-video-thumbnail>
        …
        <p class="text-white text-muted small">2.6K views • 3 days ago</p>
      </div>

* **播放地址不是直链**。``GET /api/stream/<video_id>`` 返回 HLS 主播放列表::

      #EXTM3U
      #EXT-X-VERSION:3
      #EXT-X-STREAM-INF:BANDWIDTH=5087045,RESOLUTION=1920x1080,NAME="1080"
      video_<id>_1080p.m3u8

  变体列表在 ``/api/stream/<变体名>``，分片是 **MPEG-TS**（``video/mp2t``），
  地址带 ``?token=…&expires=…``。

  因此**不需要写新的下载逻辑**：``DownloadEngine._download_hls()`` 本来就会
  从主播放列表里挑**最高码率**变体、顺序拼接分片，再用 ffmpeg 无损封装成 mp4
  （Patreon 的 mux 流走的是同一条路）。

* CDN **不校验 Referer**（实测带站点 referer、带 Patreon referer、完全不带，
  三种都能下），但仍然按站点设好，免得日后收紧。

* **没有可靠的发布日期**。列表页只有「3 days ago」这种相对时间，只有详情页
  ``/watch/<id>`` 才有「Aug 08, 2024」。把相对时间换算成绝对日期会让文件夹名
  随运行时间漂移，进而不断产生重复目录（本项目刚修过这类问题），所以这里
  **只请求列表页**（1 个请求拿全部视频），``published_at`` 留空；
  命名模板里的 ``{date}`` 会被安全地省略掉。
"""

from __future__ import annotations

import html as html_module
import re
import time
from typing import Any, Callable, Iterator
from urllib.parse import urljoin, urlparse

import requests

from .config import USER_AGENT, AppConfig
from .cookies import CookieRecord, apply_to_session
from .models import Campaign, MediaItem, PostItem
from .patreon import NotFoundError, PatreonError

SITE = "joi"
JOI_HOST = "the-joi-database.com"
JOI_BASE = "https://www.the-joi-database.com"

# 站点自报的「站点标识」前缀，允许用户写 joi:profile/<id> 这种简写
_SHORTHANDS = ("joi:", "joi-database:", "joi ")

_PROFILE_RE = re.compile(r"/profile/([0-9a-zA-Z]{8,40})")
_PLAYLIST_RE = re.compile(r"/playlist/([0-9a-zA-Z]{8,40})")
_WATCH_RE = re.compile(r"/watch/([0-9a-zA-Z]{8,40})")

# 列表项：以 video-block-<数字> 开头，切到下一个为止
_BLOCK_SPLIT_RE = re.compile(
    r'(?=<div class="[^"]*video-thumbnail-block[^"]*"\s+id="video-block-\d+")', re.I
)
_THUMB_RE = re.compile(r"<asis-video-thumbnail\b([\s\S]*?)>", re.I)
_ATTR_RE = re.compile(r'([a-zA-Z][\w-]*)\s*=\s*"([^"]*)"')
_AGE_RE = re.compile(r"<p class=\"text-white text-muted small\">([\s\S]{0,80}?)</p>", re.I)
_VIEWS_RE = re.compile(r"([\d.,]+)\s*([KMB]?)\s*views", re.I)
_RELATIVE_RE = re.compile(
    r"(\d+)\s*(second|minute|hour|day|week|month|year)s?\s*ago", re.I
)
_TAG_RE = re.compile(r"<[^>]+>")


# --------------------------------------------------------------------- 解析


def is_joi_reference(text: str) -> bool:
    """这个输入是不是指向 the-joi-database.com。"""
    low = (text or "").strip().lower()
    if not low:
        return False
    if JOI_HOST in low:
        return True
    return any(low.startswith(prefix) for prefix in _SHORTHANDS)


def parse_joi_reference(text: str) -> tuple[str, str]:
    """把 JOI 地址解析成 ``(kind, value)``。

    ``kind`` 是 ``profile`` / ``playlist`` / ``video``；取不到时抛
    :class:`PatreonError`。也接受 ``joi:<id>`` / ``joi:profile/<id>`` 简写。
    """
    raw = (text or "").strip()
    if not raw:
        raise PatreonError("请输入 JOI Database 的作者页或视频链接")

    low = raw.lower()
    for prefix in _SHORTHANDS:
        if low.startswith(prefix):
            raw = raw[len(prefix):].strip()
            low = raw.lower()
            break

    for pattern, kind in ((_PROFILE_RE, "profile"),
                          (_PLAYLIST_RE, "playlist"),
                          (_WATCH_RE, "video")):
        match = pattern.search("/" + raw.lstrip("/"))
        if match:
            return kind, match.group(1)

    # 光给一串 ID：JOI 的 ID 是 24 位十六进制
    if re.fullmatch(r"[0-9a-fA-F]{24}", raw):
        return "profile", raw

    raise PatreonError(
        f"无法从 {text!r} 里解析出 JOI Database 的地址；"
        "请粘贴作者页（/profile/…）或视频页（/watch/…）链接"
    )


def parse_duration(text: str | None) -> float | None:
    """``"11:43"`` / ``"1:02:03"`` / ``"3307.85"`` → 秒。"""
    if not text:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    if ":" not in raw:
        try:
            return float(raw)
        except ValueError:
            return None
    parts = raw.split(":")
    if len(parts) > 3:
        return None
    try:
        numbers = [float(p) for p in parts]
    except ValueError:
        return None
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60 + number
    return seconds


def parse_views(text: str | None) -> int:
    """``"2.6K views"`` → 2600。"""
    if not text:
        return 0
    match = _VIEWS_RE.search(text)
    if not match:
        return 0
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return 0
    factor = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    return int(value * factor.get(match.group(2).upper(), 1))


def parse_relative_age(text: str | None) -> str:
    """``"3 days ago"`` → ``"3 days ago"``（原样保留，只做规范化）。"""
    if not text:
        return ""
    match = _RELATIVE_RE.search(text)
    return re.sub(r"\s+", " ", match.group(0)).strip() if match else ""


def _clean(value: str | None) -> str:
    if not value:
        return ""
    text = html_module.unescape(str(value))
    return re.sub(r"\s+", " ", text).strip()


def parse_listing(html: str) -> list[dict[str, Any]]:
    """从任意含视频列表的页面里解析出条目。

    作者页、播放列表页、详情页的「相关视频」都是同一套标记，所以一个解析器
    通吃。
    """
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for block in _BLOCK_SPLIT_RE.split(html or "")[1:]:
        thumb = _THUMB_RE.search(block)
        if not thumb:
            continue
        attrs = dict(_ATTR_RE.findall(thumb.group(1)))
        video_id = (attrs.get("video-id") or "").strip()
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)

        title = _clean(attrs.get("video-title"))
        if not title:
            # 退一步：用 <h6 title="…"> 或 <h6> 里的文字
            match = re.search(r'<h6[^>]*\btitle="([^"]{0,400})"', block)
            if match:
                title = _clean(match.group(1))
            else:
                match = re.search(r"<h6[^>]*>([\s\S]{0,400}?)</h6>", block)
                title = _clean(_TAG_RE.sub("", match.group(1))) if match else ""

        meta = _AGE_RE.search(block)
        meta_text = _clean(meta.group(1)) if meta else ""
        entries.append({
            "video_id": video_id,
            "title": title or f"video-{video_id}",
            "duration": parse_duration(attrs.get("duration")),
            "duration_text": _clean(attrs.get("duration")),
            "thumbnail": attrs.get("thumbnail") or "",
            "explicit": "explicit" in attrs,
            "patreon_exclusive": "is-patreon-exclusive" in attrs,
            "views": parse_views(meta_text),
            "age": parse_relative_age(meta_text),
            "page_url": f"{JOI_BASE}/watch/{video_id}",
        })
    return entries


def parse_creator(html: str, profile_id: str = "", url: str = "") -> Campaign:
    """从作者页解析创作者信息。"""
    text = html or ""
    name = ""
    match = re.search(r'<h2[^>]*\bid="user-title"[^>]*>([\s\S]{0,300}?)</h2>', text, re.I)
    if match:
        inner = match.group(1)
        # <h2> 里通常还嵌着一个下拉按钮，名字是它前面的那段文本
        name = _clean(inner.split("<", 1)[0])
        if not name:
            name = _clean(_TAG_RE.sub(" ", inner))
    if not name:
        match = re.search(r'<meta\s+name="title"\s+content="([^"]{0,200})"', text, re.I)
        if match:
            name = _clean(match.group(1))

    image = ""
    match = re.search(
        r'(https://cdn[^"\']*?/profile_images/[^"\'\s]+)', text, re.I)
    if match:
        image = html_module.unescape(match.group(1))

    subscribers = 0
    match = re.search(r'<span id="sub[_-]counter"[^>]*>([^<]{0,20})</span>', text, re.I)
    if match:
        subscribers = parse_views(_clean(match.group(1)) + " views")

    return Campaign(
        id=profile_id,
        name=name or (f"profile-{profile_id}" if profile_id else "JOI Database"),
        vanity=name or profile_id,
        url=url or (f"{JOI_BASE}/profile/{profile_id}" if profile_id else JOI_BASE),
        summary="",
        patron_count=subscribers,
        image_url=image,
    )


_DETAIL_DATE_RE = re.compile(
    r"Views\s*•\s*([A-Z][a-z]{2})\s+(\d{1,2}),\s*(\d{4})", re.I)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def parse_detail_date(html: str) -> str:
    """详情页的 ``32,369 Views • Aug 08, 2024`` → ISO 时间串。"""
    match = _DETAIL_DATE_RE.search(html or "")
    if not match:
        return ""
    month = _MONTHS.get(match.group(1).lower()[:3])
    if not month:
        return ""
    try:
        return f"{int(match.group(3)):04d}-{month:02d}-{int(match.group(2)):02d}T00:00:00"
    except ValueError:
        return ""


def stream_url(video_id: str) -> str:
    """HLS 主播放列表地址（含全部画质档位）。"""
    return f"{JOI_BASE}/api/stream/{video_id}"


def entry_to_post(entry: dict[str, Any], campaign: Campaign) -> PostItem:
    """把一条列表记录变成 :class:`PostItem`（含一个视频媒体）。"""
    video_id = str(entry["video_id"])
    title = str(entry.get("title") or f"video-{video_id}")
    url = stream_url(video_id)
    item = MediaItem(
        url=url,
        kind="video",
        filename=f"{title}.mp4",
        mimetype="application/x-mpegURL",
        size=None,
        media_id=video_id,
        post_id=video_id,
        source="joi",
        is_preview=False,
        duration=entry.get("duration"),
        referer=JOI_BASE + "/",
        extra={
            # 下载器优先读 hls；主播放列表本身就是最高画质入口
            "hls": url,
            "site": SITE,
            "page_url": entry.get("page_url") or f"{JOI_BASE}/watch/{video_id}",
            "thumbnail": entry.get("thumbnail") or "",
            "views": int(entry.get("views") or 0),
            "age": entry.get("age") or "",
            "explicit": bool(entry.get("explicit")),
            "patreon_exclusive": bool(entry.get("patreon_exclusive")),
        },
    )
    return PostItem(
        id=video_id,
        site=SITE,
        title=title,
        campaign_id=campaign.id,
        creator=campaign.vanity or campaign.display_name,
        post_type="video_external_file",
        published_at="",          # 见模块开头的说明：列表页没有可靠日期
        url=item.extra["page_url"],
        media=[item],
        raw={"joi": dict(entry)},
    )


# --------------------------------------------------------------------- 客户端


class JoiClient:
    """the-joi-database.com 的抓取客户端。

    只做「页面 → 模型」的转换；下载交给现有的 ``DownloadEngine``。
    """

    def __init__(self, config: AppConfig, cookies: list[CookieRecord] | None = None):
        self.config = config
        self.cookies = list(cookies or [])
        self.log: Callable[[str], None] = lambda message: None
        self._session: requests.Session | None = None

    # ---------------------------------------------------------------- 会话
    def session(self) -> requests.Session:
        if self._session is None:
            session = requests.Session()
            session.headers.update({
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": JOI_BASE + "/",
            })
            if self.cookies:
                apply_to_session(session, self.cookies)
            proxy = (self.config.proxy or "").strip()
            if proxy:
                session.proxies.update({"http": proxy, "https": proxy})
            self._session = session
        return self._session

    def get_html(self, url_or_path: str) -> str:
        """取一个页面，带重试与限速。"""
        url = url_or_path
        if not url.startswith("http"):
            url = urljoin(JOI_BASE + "/", url_or_path.lstrip("/"))
        attempts = max(1, int(self.config.retries or 3))
        delay = max(0.0, float(self.config.request_delay or 0))
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self.session().get(
                    url, timeout=max(15, int(self.config.timeout or 60)))
                if response.status_code == 404:
                    raise NotFoundError(f"资源不存在：{url}")
                if response.status_code in (403, 401):
                    raise PatreonError(f"访问被拒绝（HTTP {response.status_code}）：{url}")
                if response.status_code >= 500:
                    raise PatreonError(f"服务端错误 HTTP {response.status_code}")
                response.raise_for_status()
                if delay:
                    time.sleep(delay)
                return response.text
            except NotFoundError:
                raise
            except PatreonError:
                raise
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < attempts:
                    time.sleep(min(2.0 * attempt, 8.0))
        raise PatreonError(f"请求失败：{url}（{last}）")

    # ---------------------------------------------------------------- 抓取
    def fetch_creator(self, reference: str) -> Campaign:
        """解析作者页 / 播放列表页，返回创作者。"""
        kind, value = parse_joi_reference(reference)
        if kind == "profile":
            url = f"{JOI_BASE}/profile/{value}"
            return parse_creator(self.get_html(url), value, url)
        if kind == "playlist":
            url = f"{JOI_BASE}/playlist/{value}"
            page = self.get_html(url)
            campaign = parse_creator(page, "", url)
            campaign.id = campaign.id or f"playlist-{value}"
            return campaign
        # 单个视频：从详情页里找作者
        url = f"{JOI_BASE}/watch/{value}"
        page = self.get_html(url)
        match = re.search(r'href="(/profile/[0-9a-zA-Z]{8,40})"', page)
        profile_id = ""
        if match:
            found = _PROFILE_RE.search(match.group(1))
            profile_id = found.group(1) if found else ""
        campaign = parse_creator(page, profile_id, f"{JOI_BASE}/profile/{profile_id}")
        return campaign

    def iter_posts(
        self,
        campaign_id: str,
        reference: str = "",
        options: Any = None,
        creator: str = "",
        on_page: Callable[[int, int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        max_posts: int = 0,
    ) -> Iterator[list[PostItem]]:
        """产出作品批次。作者页一次就返回全部视频，所以只有一批。"""
        kind, value = parse_joi_reference(reference or campaign_id)
        if kind == "profile":
            url = f"{JOI_BASE}/profile/{value}"
        elif kind == "playlist":
            url = f"{JOI_BASE}/playlist/{value}"
        else:
            url = f"{JOI_BASE}/watch/{value}"

        self.log(f"正在读取 JOI Database 页面：{url}")
        page = self.get_html(url)
        entries = parse_listing(page)

        if kind == "video":
            # 单篇：页面里还有「相关视频」，只保留目标那一条
            entries = [e for e in entries if e["video_id"] == value] or entries[:1]

        if max_posts:
            entries = entries[:int(max_posts)]
        if on_page:
            on_page(len(entries), len(entries))

        campaign = parse_creator(page, value if kind == "profile" else "", url)
        if creator:
            campaign.vanity = creator
        if not campaign.id:
            campaign.id = value

        posts = [entry_to_post(entry, campaign) for entry in entries]
        if posts and not (should_stop and should_stop()):
            yield posts

    def fetch_post(self, video_id: str, campaign: Campaign | None = None) -> PostItem:
        """抓单个视频（含详情页的精确日期）。"""
        url = f"{JOI_BASE}/watch/{video_id}"
        page = self.get_html(url)
        entries = parse_listing(page)
        entry = next((e for e in entries if e["video_id"] == video_id), None)
        if entry is None:
            title = ""
            match = re.search(r'data-video-title="([^"]{0,400})"', page)
            if match:
                title = _clean(match.group(1))
                title = re.sub(r"\.mp4$", "", title, flags=re.I)
            entry = {
                "video_id": video_id,
                "title": title or f"video-{video_id}",
                "duration": None,
                "thumbnail": "",
                "page_url": url,
            }
        post = entry_to_post(entry, campaign or Campaign(id=""))
        post.published_at = parse_detail_date(page)
        post.url = url
        post.media[0].extra["page_url"] = url
        return post


def host_of(url: str) -> str:
    """取 URL 的主机名（解析失败时返回空串）。"""
    try:
        return urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001
        return ""
