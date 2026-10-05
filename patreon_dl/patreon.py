"""Patreon 网页版 JSON:API 客户端。

使用内置浏览器登录后拿到的 cookie（关键是 ``session_id``）直接调用
``https://www.patreon.com/api/...``，因此能看到登录用户有权访问的付费内容。
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator
from urllib.parse import quote, urljoin, urlsplit

import requests

from .config import USER_AGENT, AppConfig
from .cookies import CookieRecord, apply_to_session, load_cookies
from .extract import ExtractOptions, build_included_index, extract_posts
from .models import Campaign, Collection, PostItem

BASE = "https://www.patreon.com"
API = BASE + "/api"

POST_INCLUDE = "campaign,user,attachments_media,images,audio,video,media,rewards"

POST_FIELDS = (
    "post_type,title,content,published_at,edited_at,url,image,embed,post_file,"
    "current_user_can_view,is_paid,is_public,min_cents_pledged_to_view,thumbnail,"
    "teaser_text,media,audio,video,comment_count,like_count,post_metadata,video_preview,"
    # 新版富文本正文：媒体以 media_id 内嵌在里面，不写进 fields 就完全拿不到
    "content_json_string"
)

# 合集接口的 include：把作品连同它们的媒体一次性取回来
COLLECTION_INCLUDE = "posts," + ",".join(
    "posts." + name for name in POST_INCLUDE.split(",")
)

CAMPAIGN_FIELDS = (
    "creation_name,is_monthly,url,summary,patron_count,pledge_sum,created_at,"
    "image_url,vanity,is_nsfw,one_liner,name"
)


class PatreonError(RuntimeError):
    """所有 Patreon 相关错误的基类。"""


class AuthError(PatreonError):
    """未登录或登录已过期。"""


class NotFoundError(PatreonError):
    """创作者 / 作品不存在或不可见。"""


class RateLimited(PatreonError):
    """被限流，需要稍后重试。"""


@dataclass
class Page:
    posts: list[PostItem]
    next_cursor: str | None
    total: int


class PatreonClient:
    """带重试、限速与分页的 Patreon 客户端。"""

    def __init__(self, config: AppConfig | None = None, session: requests.Session | None = None):
        self.config = config or AppConfig()
        self.session = session or requests.Session()
        self.session.headers.update(self._default_headers())
        self._last_request = 0.0
        self.log: Callable[[str], None] = lambda message: None
        self._media_cache: dict[str, dict[str, Any]] = {}
        self._media_fetched = 0
        # 每取到一个正文内嵌媒体就回调一次，用于界面显示进度
        self.on_media_progress: Callable[[int], None] | None = None

    # ------------------------------------------------------------- 基础设施
    def _default_headers(self) -> dict[str, str]:
        return {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "x-requested-with": "XMLHttpRequest",
            "Origin": BASE,
            "Referer": BASE + "/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

    def load_saved_session(self) -> int:
        """从磁盘恢复上次登录的 cookie。"""
        records = load_cookies()
        count = apply_to_session(self.session, records)
        if count:
            self.log(f"已载入 {count} 条已保存的登录凭证")
        return count

    def set_cookies(self, records: list[CookieRecord]) -> int:
        return apply_to_session(self.session, records)

    def apply_proxy(self) -> None:
        proxy = (self.config.proxy or "").strip()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        else:
            self.session.proxies.clear()

    def _throttle(self) -> None:
        delay = max(0.0, float(self.config.request_delay or 0))
        if delay <= 0:
            return
        elapsed = time.monotonic() - self._last_request
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_request = time.monotonic()

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        expect_json: bool = True,
        referer: str | None = None,
        stream: bool = False,
    ) -> Any:
        """带重试的请求。失败抛出 :class:`PatreonError` 子类。"""
        self.apply_proxy()
        headers = {}
        if referer:
            headers["Referer"] = referer
        last_error: Exception | None = None
        attempts = max(1, int(self.config.retries) + 1)

        for attempt in range(attempts):
            self._throttle()
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    timeout=self.config.timeout,
                    stream=stream,
                    allow_redirects=True,
                )
            except requests.RequestException as exc:
                last_error = exc
                self.log(f"网络错误（第 {attempt + 1}/{attempts} 次）：{exc}")
                time.sleep(min(2 ** attempt, 10) + random.random())
                continue

            if response.status_code == 429:
                wait = float(response.headers.get("Retry-After") or (5 * (attempt + 1)))
                self.log(f"被限流，等待 {wait:.0f} 秒后重试")
                time.sleep(min(wait, 120))
                last_error = RateLimited("请求过于频繁，已被 Patreon 限流")
                continue

            if response.status_code in (401, 403):
                raise AuthError("登录状态无效或已过期，请重新登录 Patreon")

            if response.status_code == 404:
                raise NotFoundError(f"资源不存在：{url}")

            if response.status_code >= 500:
                last_error = PatreonError(f"服务器错误 {response.status_code}")
                self.log(f"服务端错误 {response.status_code}，稍后重试")
                time.sleep(min(2 ** attempt, 15) + random.random())
                continue

            if response.status_code >= 400:
                detail = response.text[:300].replace("\n", " ")
                raise PatreonError(f"请求失败 {response.status_code}：{detail}")

            if not expect_json:
                return response

            try:
                return response.json()
            except json.JSONDecodeError as exc:
                text = response.text.strip()
                if text.startswith("<"):
                    raise PatreonError(
                        "服务器返回了网页而不是数据，通常是登录已失效或触发了人机验证"
                    ) from exc
                raise PatreonError(f"返回内容不是合法 JSON：{text[:200]}") from exc

        if isinstance(last_error, PatreonError):
            raise last_error
        raise PatreonError(f"请求失败：{last_error}")

    # ------------------------------------------------------------- 会话检查
    def current_user(self) -> dict[str, Any] | None:
        """返回当前登录用户，未登录返回 None。"""
        try:
            payload = self.request(
                "GET",
                f"{API}/current_user",
                params={
                    "include": "campaign",
                    "fields[user]": "full_name,vanity,email,image_url,is_email_verified,url",
                },
            )
        except (AuthError, NotFoundError):
            return None
        except PatreonError:
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return None
        attrs = data.get("attributes") or {}
        return {
            "id": data.get("id"),
            "name": attrs.get("full_name") or attrs.get("vanity") or "",
            "vanity": attrs.get("vanity") or "",
            "email": attrs.get("email") or "",
            "image_url": attrs.get("image_url") or "",
        }

    # --------------------------------------------------------- 媒体对象
    def get_media(self, media_id: str) -> dict[str, Any] | None:
        """按 ID 取单个媒体对象（带缓存）。

        正文富文本（``content_json_string``）里内嵌的媒体只给了 ``media_id``，
        不属于任何 relationship，只能这样单独取。响应里含 ``download_url``，
        和通过 relationship 拿到的媒体对象结构一致。
        """
        media_id = str(media_id or "").strip()
        if not media_id:
            return None
        cached = self._media_cache.get(media_id)
        if cached is not None:
            return cached or None
        try:
            payload = self.request(
                "GET", f"{API}/media/{media_id}", referer=f"{BASE}/"
            )
        except NotFoundError:
            self._media_cache[media_id] = {}
            return None
        except PatreonError as exc:
            self.log(f"取媒体 {media_id} 失败：{exc}")
            self._media_cache[media_id] = {}
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            self._media_cache[media_id] = {}
            return None
        self._media_cache[media_id] = data
        self._media_fetched += 1
        if self.on_media_progress:
            self.on_media_progress(self._media_fetched)
        return data

    def make_media_resolver(
        self, included: dict[tuple[str, str], dict[str, Any]]
    ) -> Callable[[str], dict[str, Any] | None]:
        """生成一个解析器：优先用页面里已带的媒体，没有再单独请求。"""

        def resolve(media_id: str) -> dict[str, Any] | None:
            obj = included.get(("media", str(media_id)))
            if isinstance(obj, dict):
                return obj
            return self.get_media(str(media_id))

        return resolve

    # --------------------------------------------------------- 合集（Collections）
    def list_collections(self, campaign_id: str) -> list[Collection]:
        """列出某个创作者的全部合集。

        ``GET /api/collection?filter[campaign_id]=…`` —— 注意路径是**单数**
        ``collection``，复数形式会 404。
        """
        campaign_id = str(campaign_id or "").strip()
        if not campaign_id:
            return []
        try:
            payload = self.request(
                "GET",
                f"{API}/collection",
                params={"filter[campaign_id]": campaign_id},
                referer=f"{BASE}/",
            )
        except NotFoundError:
            return []
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        return [
            self._collection_from(obj, campaign_id)
            for obj in items
            if isinstance(obj, dict) and obj.get("id")
        ]

    def get_collection(self, collection_id: str) -> Collection:
        """取单个合集（含 ``post_ids``，不含作品内容）。"""
        payload = self.request(
            "GET",
            f"{API}/collection/{collection_id}",
            params={"include": "campaign"},
            referer=f"{BASE}/",
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise NotFoundError(f"找不到合集 {collection_id}")
        included = build_included_index(payload)
        rel = ((data.get("relationships") or {}).get("campaign") or {}).get("data") or {}
        campaign_id = str(rel.get("id") or "") if isinstance(rel, dict) else ""
        if not campaign_id:
            for (obj_type, obj_id) in included:
                if obj_type == "campaign":
                    campaign_id = obj_id
                    break
        return self._collection_from(data, campaign_id)

    def fetch_collection_posts(
        self,
        collection_id: str,
        options: ExtractOptions | None = None,
        creator: str = "",
    ) -> tuple[Collection, list[PostItem], Campaign | None]:
        """取一个合集的全部作品（含媒体）—— **一次请求**搞定。

        ``GET /api/collection/{id}?include=posts,posts.images,…``
        作品会按创作者自定义的顺序（``post_sort_type``）返回，本方法会重新排好序。
        """
        payload = self.request(
            "GET",
            f"{API}/collection/{collection_id}",
            params={"include": COLLECTION_INCLUDE, "fields[post]": POST_FIELDS},
            referer=f"{BASE}/",
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise NotFoundError(f"找不到合集 {collection_id}")

        included = build_included_index(payload)
        rel = ((data.get("relationships") or {}).get("campaign") or {}).get("data") or {}
        campaign_id = str(rel.get("id") or "") if isinstance(rel, dict) else ""
        if not campaign_id:
            for (obj_type, obj_id) in included:
                if obj_type == "campaign":
                    campaign_id = obj_id
                    break

        collection = self._collection_from(data, campaign_id)

        # 作者名
        creator_name = creator.strip()
        if not creator_name:
            for obj in payload.get("included") or []:
                if obj.get("type") == "user":
                    attrs = obj.get("attributes") or {}
                    creator_name = str(attrs.get("full_name") or attrs.get("vanity") or "")
                    if creator_name:
                        break

        post_objs = [o for o in (payload.get("included") or [])
                     if isinstance(o, dict) and o.get("type") == "post"]
        # 按合集里创作者排的顺序重排
        order = {str(pid): index for index, pid in enumerate(collection.post_ids)}
        post_objs.sort(key=lambda obj: order.get(str(obj.get("id")), len(order) + 1))

        resolver = (
            self.make_media_resolver(included) if self.config.fetch_inline_media else None
        )
        posts = extract_posts(
            {"data": post_objs, "included": payload.get("included")},
            options, campaign_id, creator_name, media_resolver=resolver,
        )
        campaign = self._campaign_from_included(included, campaign_id, creator_name)
        return collection, posts, campaign

    @staticmethod
    def _collection_from(obj: dict[str, Any], campaign_id: str = "") -> Collection:
        attrs = obj.get("attributes") if isinstance(obj.get("attributes"), dict) else {}
        thumb = attrs.get("thumbnail") if isinstance(attrs.get("thumbnail"), dict) else {}
        thumb_url = ""
        for key in ("default", "original", "url", "thumbnail"):
            value = thumb.get(key)
            if isinstance(value, str) and value.startswith("http"):
                thumb_url = value
                break
        return Collection(
            id=str(obj.get("id") or ""),
            title=str(attrs.get("title") or ""),
            description=str(attrs.get("description") or ""),
            campaign_id=str(campaign_id or ""),
            post_count=int(attrs.get("num_posts") or 0),
            post_ids=[str(x) for x in (attrs.get("post_ids") or [])],
            thumbnail=thumb_url,
            created_at=str(attrs.get("created_at") or ""),
            sort_type=str(attrs.get("post_sort_type") or ""),
        )

    # --------------------------------------------------------- 创作者解析
    def resolve_campaign(self, reference: str) -> Campaign:
        """支持：纯数字 ID、creator 主页 URL、vanity 名称、作品 URL。

        URL 形式会给出多个候选创作者名（Patreon 有 ``/c/``、``/cw/``、
        ``/user`` 等多种前缀），按顺序逐个尝试，第一个成功的就用它。
        """
        reference = (reference or "").strip()
        if not reference:
            raise PatreonError("请输入创作者主页地址或名字")

        kind, values = parse_reference_all(reference)
        if not values:
            raise PatreonError(f"无法从 {reference} 里解析出创作者信息")
        if kind == "campaign_id":
            return self.get_campaign(values[0])
        if kind == "post":
            return self.campaign_of_post(values[0])
        if kind == "user_id":
            return self.campaign_of_user(values[0])
        if kind == "collection":
            collection = self.get_collection(values[0])
            if not collection.campaign_id:
                raise NotFoundError(f"合集 {values[0]} 没有关联到任何创作者")
            return self.get_campaign(collection.campaign_id)

        problems: list[str] = []
        for vanity in values:
            try:
                return self.campaign_of_vanity(vanity)
            except PatreonError as exc:
                problems.append(f"  · /{vanity} → {exc}")
        raise NotFoundError(
            "无法解析这个地址对应的创作者。\n"
            "已尝试：\n" + "\n".join(problems) + "\n\n"
            "建议改用创作者主页地址（例如 https://www.patreon.com/cw/BBebe）、\n"
            "任意一篇作品链接，或创作者的数字 ID。"
        )

    def get_campaign(self, campaign_id: str) -> Campaign:
        payload = self.request(
            "GET",
            f"{API}/campaigns/{campaign_id}",
            params={
                "include": "creator,rewards",
                "fields[campaign]": CAMPAIGN_FIELDS,
            },
            referer=f"{BASE}/",
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise NotFoundError(f"找不到创作者（campaign {campaign_id}）")
        attrs = data.get("attributes") or {}
        vanity = attrs.get("vanity") or ""
        return Campaign(
            id=str(data.get("id") or campaign_id),
            name=attrs.get("creation_name") or attrs.get("name") or vanity,
            vanity=vanity,
            url=attrs.get("url") or (f"{BASE}/{vanity}" if vanity else f"{BASE}/"),
            summary=attrs.get("summary") or "",
            patron_count=int(attrs.get("patron_count") or 0),
            image_url=attrs.get("image_url") or "",
            created_at=attrs.get("created_at") or "",
        )

    def campaign_of_vanity(self, vanity: str) -> Campaign:
        """从创作者主页 HTML 里解析 campaign id。"""
        vanity = vanity.strip().strip("/")
        if not vanity:
            raise NotFoundError("创作者名为空")
        url = f"{BASE}/{quote(vanity)}"
        try:
            response = self.request("GET", url, expect_json=False)
        except NotFoundError as exc:
            raise NotFoundError(f"Patreon 上没有 /{vanity} 这个页面（HTTP 404）") from exc
        html = response.text or ""

        campaign_id = self._campaign_id_from_html(html)
        if not campaign_id:
            # 主页可能跳转到了别的路径（例如 /BBebe → /cw/BBebe），
            # 用最终地址的最后一段再试一次
            final = str(getattr(response, "url", "") or "")
            final_segment = urlsplit(final).path.strip("/").split("/")[-1]
            if final_segment and final_segment != vanity:
                retry = self.request("GET", f"{BASE}/{quote(final_segment)}", expect_json=False)
                campaign_id = self._campaign_id_from_html(retry.text or "")
        if not campaign_id:
            raise NotFoundError(
                f"打开了 {url}，但页面里找不到创作者 ID。\n"
                "这通常说明该地址不是创作者主页，"
                "请改用主页地址、任意一篇作品链接，或创作者的数字 ID。"
            )
        return self.get_campaign(campaign_id)

    @staticmethod
    def _campaign_id_from_html(html: str) -> str | None:
        patterns = (
            r'"campaign"\s*:\s*\{\s*"data"\s*:\s*\{\s*"id"\s*:\s*"(\d+)"',
            r'"campaignId"\s*:\s*"?(\d+)"?',
            r'"campaign_id"\s*:\s*"?(\d+)"?',
            r'/api/campaigns/(\d+)',
            r'"id"\s*:\s*"(\d{4,})"\s*,\s*"type"\s*:\s*"campaign"',
        )
        for pattern in patterns:
            found = re.findall(pattern, html)
            if found:
                # 取出现次数最多的那个，避免抓到无关 ID
                from collections import Counter

                return Counter(found).most_common(1)[0][0]
        return None

    def campaign_of_post(self, post_id: str) -> Campaign:
        payload = self.request(
            "GET",
            f"{API}/posts/{post_id}",
            params={"include": "campaign", "fields[campaign]": CAMPAIGN_FIELDS},
            referer=f"{BASE}/",
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise NotFoundError(f"找不到作品 {post_id}")
        rel = ((data.get("relationships") or {}).get("campaign") or {}).get("data") or {}
        campaign_id = rel.get("id")
        if not campaign_id:
            # 从 included 里找
            for obj in payload.get("included") or []:
                if obj.get("type") == "campaign":
                    campaign_id = obj.get("id")
                    break
        if not campaign_id:
            raise NotFoundError("无法从该作品定位到创作者")
        return self.get_campaign(str(campaign_id))

    def campaign_of_user(self, user_id: str) -> Campaign:
        payload = self.request(
            "GET",
            f"{API}/user/{user_id}",
            params={"include": "campaign", "fields[campaign]": CAMPAIGN_FIELDS},
            referer=f"{BASE}/",
        )
        for obj in payload.get("included") or []:
            if obj.get("type") == "campaign":
                return self.get_campaign(str(obj.get("id")))
        data = payload.get("data") if isinstance(payload, dict) else {}
        rel = ((data.get("relationships") or {}).get("campaign") or {}).get("data") or {}
        if rel.get("id"):
            return self.get_campaign(str(rel["id"]))
        raise NotFoundError(f"该用户没有可见的创作者主页（user {user_id}）")

    # --------------------------------------------------------------- 作品
    def fetch_page(
        self,
        campaign_id: str,
        cursor: str | None = None,
        page_size: int | None = None,
        options: ExtractOptions | None = None,
        creator: str = "",
    ) -> Page:
        size = int(page_size or self.config.page_size or 25)
        size = max(1, min(size, 100))
        params: dict[str, Any] = {
            "include": POST_INCLUDE,
            "fields[post]": POST_FIELDS,
            "filter[is_draft]": "false",
            "filter[campaign_id]": str(campaign_id),
            "sort": "-published_at",
            "page[count]": str(size),
        }
        if cursor:
            params["page[cursor]"] = cursor

        payload = self.request(
            "GET",
            f"{API}/posts",
            params=params,
            referer=f"{BASE}/",
        )
        included = build_included_index(payload)
        resolver = (
            self.make_media_resolver(included) if self.config.fetch_inline_media else None
        )
        posts = extract_posts(payload, options, campaign_id=str(campaign_id),
                              creator=creator, media_resolver=resolver)
        meta = payload.get("meta") or {}
        pagination = meta.get("pagination") or {}
        cursors = pagination.get("cursors") or {}
        total = int(pagination.get("total") or 0)
        return Page(posts=posts, next_cursor=cursors.get("next"), total=total)

    def iter_posts(
        self,
        campaign_id: str,
        *,
        max_posts: int = 0,
        options: ExtractOptions | None = None,
        creator: str = "",
        on_page: Callable[[int, int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> Iterator[list[PostItem]]:
        """按页产出作品列表，处理分页与停止信号。"""
        cursor: str | None = None
        fetched = 0
        total = 0
        while True:
            if should_stop and should_stop():
                return
            page = self.fetch_page(campaign_id, cursor, options=options, creator=creator)
            total = page.total or total
            if not page.posts:
                return
            fetched += len(page.posts)
            if on_page:
                on_page(fetched, total)
            yield page.posts
            if max_posts and fetched >= max_posts:
                return
            if not page.next_cursor:
                return
            cursor = page.next_cursor

    def get_post(
        self,
        post_id: str,
        options: ExtractOptions | None = None,
        campaign_id: str = "",
        creator: str = "",
    ) -> PostItem:
        payload = self.request(
            "GET",
            f"{API}/posts/{post_id}",
            params={"include": POST_INCLUDE, "fields[post]": POST_FIELDS},
            referer=f"{BASE}/",
        )
        included = build_included_index(payload)
        from .extract import extract_post

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise NotFoundError(f"找不到作品 {post_id}")
        resolver = (
            self.make_media_resolver(included) if self.config.fetch_inline_media else None
        )
        return extract_post(data, included, options, campaign_id, creator,
                            media_resolver=resolver)

    def fetch_post(
        self,
        post_id: str,
        options: ExtractOptions | None = None,
        creator: str = "",
    ) -> tuple[PostItem, Campaign | None]:
        """只抓单篇作品：一次请求同时拿到作品内容和它所属的创作者。

        创作者信息直接取自同一份响应里的 ``included``，不再额外请求 campaign 接口，
        所以「只抓这一篇」模式总共只花 1 个请求。
        """
        payload = self.request(
            "GET",
            f"{API}/posts/{post_id}",
            params={"include": POST_INCLUDE, "fields[post]": POST_FIELDS},
            referer=f"{BASE}/",
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise NotFoundError(f"找不到作品 {post_id}")

        included = build_included_index(payload)
        from .extract import extract_post

        # 所属创作者
        campaign_id = ""
        rel = ((data.get("relationships") or {}).get("campaign") or {}).get("data") or {}
        if isinstance(rel, dict) and rel.get("id"):
            campaign_id = str(rel["id"])
        if not campaign_id:
            for (obj_type, obj_id) in included:
                if obj_type == "campaign":
                    campaign_id = obj_id
                    break

        # 作者名：优先用作品属性里的 user，其次响应里的 user 对象
        creator_name = creator.strip()
        user_rel = ((data.get("relationships") or {}).get("user") or {}).get("data") or {}
        if not creator_name and isinstance(user_rel, dict) and user_rel.get("id"):
            user_obj = included.get(("user", str(user_rel["id"])))
            if user_obj:
                attrs = user_obj.get("attributes") or {}
                creator_name = str(attrs.get("full_name") or attrs.get("vanity") or "")
        if not creator_name:
            for obj in payload.get("included") or []:
                if obj.get("type") == "user":
                    attrs = obj.get("attributes") or {}
                    creator_name = str(attrs.get("full_name") or attrs.get("vanity") or "")
                    if creator_name:
                        break

        campaign = self._campaign_from_included(included, campaign_id, creator_name)
        resolver = (
            self.make_media_resolver(included) if self.config.fetch_inline_media else None
        )
        post = extract_post(data, included, options, campaign_id, creator_name,
                            media_resolver=resolver)
        return post, campaign

    @staticmethod
    def _campaign_from_included(
        included: dict[tuple[str, str], dict[str, Any]],
        campaign_id: str,
        fallback_name: str = "",
    ) -> Campaign | None:
        """用作品响应里自带的 campaign 对象拼一个 :class:`Campaign`。"""
        if not campaign_id:
            return None
        obj = included.get(("campaign", str(campaign_id)))
        attrs = (obj or {}).get("attributes") or {}
        vanity = str(attrs.get("vanity") or fallback_name or "")
        name = str(attrs.get("creation_name") or attrs.get("name") or fallback_name or "")
        return Campaign(
            id=str(campaign_id),
            name=name or f"campaign-{campaign_id}",
            vanity=vanity,
            url=str(attrs.get("url") or (f"{BASE}/{vanity}" if vanity else BASE)),
            summary=str(attrs.get("summary") or ""),
            patron_count=int(attrs.get("patron_count") or 0),
            image_url=str(attrs.get("image_url") or ""),
            created_at=str(attrs.get("created_at") or ""),
        )


# ------------------------------------------------------------------ 解析输入

# Patreon 用来区分页面类型的路径前缀，它们不是创作者名。
# 例如 /cw/BBebe、/c/rossdraws、/user?u=123 里的 cw / c / user。
NON_VANITY_SEGMENTS = {
    "c", "cw", "creator", "creators", "user", "users", "profile",
    "join", "bepatron", "membership", "memberships", "pricing",
    "posts", "post", "api", "login", "signup", "sign-up", "logout",
    "explore", "search", "settings", "messages", "notifications",
    "home", "about", "gift", "checkout", "me", "my", "discover",
    "collection", "collections", "tag", "tags", "genre",
}


def parse_reference_all(reference: str) -> tuple[str, list[str]]:
    """把用户输入归类为 (kind, [候选值])。

    kind ∈ {campaign_id, post, user_id, vanity}。
    vanity 会给出多个候选（去掉前缀后的、以及原始路径段），
    由调用方逐个尝试，这样 Patreon 新增路径前缀时也不会立刻失效。
    """
    reference = (reference or "").strip()
    if not reference:
        return "vanity", []

    # 纯数字 = campaign id
    if re.fullmatch(r"\d+", reference):
        return "campaign_id", [reference]

    # 纯名字（没有协议、没有斜杠、不是 patreon 域名）
    if "patreon.com" not in reference.lower() and "/" not in reference:
        name = reference.lstrip("@").strip()
        return ("vanity", [name]) if name else ("vanity", [])

    if not reference.lower().startswith(("http://", "https://")):
        reference = "https://" + reference.lstrip("/")

    parts = urlsplit(reference)
    path = parts.path.strip("/")
    query = parts.query

    # /posts/<slug>-<id>  或  /<vanity>/posts/<slug>-<id>
    match = re.search(r"/posts/(?:[^/?#]*-)?(\d+)", "/" + path)
    if match:
        return "post", [match.group(1)]

    # /collection/<id> —— 合集（注意是单数 collection）
    match = re.search(r"/collection/(\d+)", "/" + path)
    if match:
        return "collection", [match.group(1)]

    # /<vanity>/collections —— 想浏览该创作者的全部合集
    segments = [s for s in path.split("/") if s]
    if len(segments) >= 2 and segments[-1].lower() == "collections":
        head = [s for s in segments[:-1] if s.lower() not in NON_VANITY_SEGMENTS]
        tail = [s for s in segments[:-1] if s not in head]
        if head or tail:
            return "collections", head + tail

    # /user?u=<id>
    match = re.search(r"(?:^|[?&])u=(\d+)", query)
    if match:
        return "user_id", [match.group(1)]

    if re.fullmatch(r"user/?", path):
        raise PatreonError("这个地址指向用户页但没有 u= 参数，请改用创作者主页地址")

    segments = [s for s in path.split("/") if s]
    if not segments:
        return "vanity", []

    # 去掉开头连续出现的非创作者路径段（可能是 /c/cw/xxx 这种组合）
    trimmed = list(segments)
    while trimmed and trimmed[0].lower() in NON_VANITY_SEGMENTS:
        trimmed.pop(0)

    candidates: list[str] = []
    for value in trimmed + segments:
        if value and value not in candidates:
            candidates.append(value)
    return "vanity", candidates


def parse_reference(reference: str) -> tuple[str, str]:
    """返回最可能的一种解析结果 (kind, value)。"""
    kind, values = parse_reference_all(reference)
    return kind, (values[0] if values else "")


def absolute(url: str, base: str = BASE) -> str:
    return urljoin(base, url)
