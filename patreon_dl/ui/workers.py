"""后台工作线程：抓取作品、下载媒体、校验登录、加载预览图。"""

from __future__ import annotations

import os

from PySide6.QtCore import QThread, Signal

from ..config import AppConfig
from ..cookies import CookieRecord
from ..downloader import DownloadEngine, Task, TaskResult, build_tasks, summarize
from ..extract import ExtractOptions
from ..joi import JoiClient, is_joi_reference
from ..models import Campaign, PostItem
from ..patreon import AuthError, PatreonClient, PatreonError
from ..state import StateStore


def clone_config(config: AppConfig) -> AppConfig:
    """拷贝一份配置，避免工作线程读到界面正在修改的对象。"""
    return AppConfig.from_dict(config.to_dict())


class FetchWorker(QThread):
    """解析创作者并分页抓取作品列表；也可以只抓单篇作品或某个合集。"""

    campaign_ready = Signal(object)      # Campaign
    collections_ready = Signal(object)   # list[Collection]
    collection_ready = Signal(object)    # Collection —— 让界面选中它
    posts_batch = Signal(object)         # list[PostItem]
    page_progress = Signal(int, int)     # 已抓取, 总数
    inline_progress = Signal(int)        # 已取回多少个「正文内嵌媒体」
    log = Signal(str)                    # 过程提示（例如「正在读取页面」）
    failed = Signal(str)
    completed = Signal(int)              # 累计作品数

    def __init__(self, config: AppConfig, cookies: list[CookieRecord], reference: str,
                 options: ExtractOptions, single_post_id: str | None = None,
                 collection_id: str | None = None, fetch_collections: bool = True,
                 parent=None):
        super().__init__(parent)
        self.config = clone_config(config)
        self.cookies = cookies
        self.reference = reference
        self.options = options
        self.single_post_id = (single_post_id or "").strip() or None
        self.collection_id = (collection_id or "").strip() or None
        self.fetch_collections = fetch_collections
        self._stop = False
        self._total = 0

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:  # noqa: D102
        try:
            if is_joi_reference(self.reference) or is_joi_reference(self.collection_id or ""):
                self._run_joi()
                return

            client = PatreonClient(self.config)
            client.set_cookies(self.cookies)
            client.log = self.log.emit
            # 正文里内嵌的媒体要逐个额外请求，这里把进度报给界面
            client.on_media_progress = lambda count: self.inline_progress.emit(count)

            if self.single_post_id:
                self._run_single(client)
                return
            if self.collection_id:
                self._run_collection(client)
                return

            campaign = client.resolve_campaign(self.reference)
            self.campaign_ready.emit(campaign)
            self._emit_collections(client, campaign.id)

            for batch in client.iter_posts(
                campaign.id,
                max_posts=int(self.config.max_posts or 0),
                options=self.options,
                creator=campaign.vanity or campaign.display_name,
                on_page=lambda fetched, total: self.page_progress.emit(fetched, total),
                should_stop=lambda: self._stop,
            ):
                if self._stop:
                    break
                self._total += len(batch)
                self.posts_batch.emit(batch)

            self.completed.emit(self._total)
        except AuthError as exc:
            self.failed.emit(f"登录已失效：{exc}")
        except PatreonError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"抓取失败：{exc}")

    def _emit_collections(self, client: PatreonClient, campaign_id: str) -> None:
        """顺带把创作者的合集列出来，失败不影响主流程。"""
        if not self.fetch_collections or not campaign_id:
            return
        try:
            collections = client.list_collections(campaign_id)
        except PatreonError:
            return
        if collections:
            self.collections_ready.emit(collections)

    # ------------------------------------------------------------ JOI Database
    def _run_joi(self) -> None:
        """抓取 the-joi-database.com 的作者页 / 视频页。

        与 Patreon 不同，作者页**一次就列出全部视频**，所以只有一批结果。
        每个视频的媒体地址指向站点的 HLS 主播放列表，下载器会自动挑最高码率
        变体、拼分片、用 ffmpeg 无损封装成 mp4。
        """
        reference = self.reference or (self.collection_id or "")
        client = JoiClient(self.config, self.cookies)
        client.log = self.log.emit

        self.page_progress.emit(0, 1)
        campaign = client.fetch_creator(reference)
        self.campaign_ready.emit(campaign)

        total = 0
        for batch in client.iter_posts(
            campaign.id,
            reference=reference,
            options=self.options,
            creator=campaign.vanity,
            on_page=lambda fetched, all_count: self.page_progress.emit(fetched, all_count),
            should_stop=lambda: self._stop,
            max_posts=int(self.config.max_posts or 0),
        ):
            if self._stop:
                break
            total += len(batch)
            self.posts_batch.emit(batch)

        self.completed.emit(total)

    def _run_single(self, client: PatreonClient) -> None:
        """只抓取指定的一篇作品（只需 1 个请求）。"""
        post_id = str(self.single_post_id)
        self.page_progress.emit(0, 1)
        post, campaign = client.fetch_post(post_id, self.options)
        if campaign is None:
            campaign = Campaign(
                id=post.campaign_id or "",
                name=post.creator or f"campaign-{post.campaign_id}",
                vanity=post.creator or "",
            )
        self.campaign_ready.emit(campaign)
        self.posts_batch.emit([post])
        self._total = 1
        self.page_progress.emit(1, 1)
        self.completed.emit(1)

    def _run_collection(self, client: PatreonClient) -> None:
        """只抓某个合集 —— 一次请求就能拿回它的全部作品（含媒体）。"""
        collection_id = str(self.collection_id)
        self.page_progress.emit(0, 1)
        collection, posts, campaign = client.fetch_collection_posts(
            collection_id, self.options
        )
        if campaign is None:
            campaign = Campaign(
                id=collection.campaign_id or "",
                name=f"campaign-{collection.campaign_id}",
                vanity="",
            )
        self.campaign_ready.emit(campaign)
        self._emit_collections(client, campaign.id)
        self.collection_ready.emit(collection)
        if posts:
            self.posts_batch.emit(posts)
        self._total = len(posts)
        self.page_progress.emit(len(posts), max(len(posts), collection.post_count))
        self.completed.emit(len(posts))


class SessionCheckWorker(QThread):
    """校验当前 cookie 是否有效。"""

    result = Signal(object)   # dict | None
    failed = Signal(str)

    def __init__(self, config: AppConfig, cookies: list[CookieRecord], parent=None):
        super().__init__(parent)
        self.config = clone_config(config)
        self.cookies = cookies

    def run(self) -> None:  # noqa: D102
        try:
            client = PatreonClient(self.config)
            client.set_cookies(self.cookies)
            self.result.emit(client.current_user())
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class DownloadWorker(QThread):
    """执行下载队列，把引擎回调转发成 Qt 信号。"""

    task_started = Signal(object)
    task_progress = Signal(object, int, int)
    task_done = Signal(object)
    queue_progress = Signal(int, int)
    log = Signal(str)
    completed = Signal(object)

    def __init__(self, config: AppConfig, cookies: list[CookieRecord], state: StateStore,
                 campaign: Campaign, posts: list[PostItem], only_posts: set[str] | None,
                 parent=None):
        super().__init__(parent)
        self.config = clone_config(config)
        self.cookies = cookies
        self.state = state
        self.campaign = campaign
        self.posts = posts
        self.only_posts = only_posts
        self.engine: DownloadEngine | None = None
        self.tasks: list[Task] = []

    def stop(self) -> None:
        if self.engine:
            self.engine.stop()

    def run(self) -> None:  # noqa: D102
        try:
            from ..metadata import write_post_files

            self.tasks = build_tasks(
                self.config, self.campaign, self.posts, self.state,
                only_posts=self.only_posts,
                on_log=self.log.emit,
            )
            if not self.tasks:
                self.completed.emit(summarize([]))
                return

            # 需要写元数据的作品目录，先记录，下载完成后统一写
            written_dirs: set[tuple[str, str]] = set()

            self.engine = DownloadEngine(
                self.config, self.state, self.cookies,
                on_task_start=self.task_started.emit,
                on_task_progress=lambda task, done, total: self.task_progress.emit(task, done, total),
                on_task_done=self.task_done.emit,
                on_log=self.log.emit,
                on_queue_progress=self.queue_progress.emit,
            )
            results: list[TaskResult] = self.engine.run(self.tasks)

            # 写出 post.txt / post.json
            if self.config.write_metadata or self.config.write_post_text:
                for task in self.tasks:
                    marker = (task.post.id, task.dest_dir)
                    if marker in written_dirs:
                        continue
                    written_dirs.add(marker)
                    try:
                        write_post_files(
                            self.config, self.campaign, task.post, task.dest_dir,
                            media=[m for m in task.post.media],
                        )
                    except OSError as exc:
                        self.log.emit(f"写入元数据失败：{exc}")

            summary = summarize(results)
            self.completed.emit(summary)
        except Exception as exc:  # noqa: BLE001
            self.log.emit(f"下载线程异常：{exc}")
            self.completed.emit(summarize([]))


class PreviewLoader(QThread):
    """后台下载一张预览图。"""

    loaded = Signal(object, object)   # url, bytes | None

    def __init__(self, url: str, referer: str = "", proxy: str = "", parent=None):
        super().__init__(parent)
        self.url = url
        self.referer = referer
        self.proxy = proxy

    def run(self) -> None:  # noqa: D102
        try:
            import requests

            from ..config import USER_AGENT

            headers = {"User-Agent": USER_AGENT, "Accept": "image/*,*/*"}
            if self.referer:
                headers["Referer"] = self.referer
            proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
            response = requests.get(self.url, headers=headers, timeout=30, proxies=proxies)
            if response.status_code == 200:
                self.loaded.emit(self.url, response.content[:12 * 1024 * 1024])
            else:
                self.loaded.emit(self.url, None)
        except Exception:  # noqa: BLE001
            self.loaded.emit(self.url, None)


def open_in_explorer(path: str) -> None:
    """在系统文件管理器里打开目录。"""
    import subprocess
    import sys

    if not path or not os.path.isdir(path):
        return
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:  # noqa: BLE001
        pass
