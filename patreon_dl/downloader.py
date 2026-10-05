"""多线程下载引擎：断点续传、重试、HLS 兜底、可选 yt-dlp 外链下载。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.parse import urljoin

import requests

from .config import USER_AGENT, AppConfig
from .cookies import CookieRecord, apply_to_session
from .models import Campaign, MediaItem, PostItem
from .state import StateStore
from .util import guess_extension, human_size

HLS_HINTS = (".m3u8",)
STATUS_DONE = "done"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUS_STOPPED = "stopped"


@dataclass
class Task:
    """一次下载任务。"""

    item: MediaItem
    post: PostItem
    campaign: Campaign
    dest_dir: str
    filename: str
    index: int = 1

    @property
    def campaign_id(self) -> str:
        return self.campaign.id

    @property
    def key(self) -> str:
        return self.item.key

    @property
    def dest_path(self) -> str:
        return os.path.join(self.dest_dir, self.filename)

    def label(self) -> str:
        return f"{self.post.safe_title[:48]} / {self.filename}"


@dataclass
class TaskResult:
    task: Task
    status: str = STATUS_DONE
    path: str = ""
    size: int = 0
    error: str = ""
    seconds: float = 0.0
    resumed: bool = False
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_DONE, STATUS_SKIPPED)


class DownloadError(RuntimeError):
    pass


class StopRequested(RuntimeError):
    pass


class DownloadEngine:
    """把一个任务列表下载到磁盘。

    回调都在工作线程里触发；GUI 层用 Qt 信号转发即可（Qt 信号跨线程是安全的）。
    """

    def __init__(
        self,
        config: AppConfig,
        state: StateStore,
        cookies: Iterable[CookieRecord] | None = None,
        *,
        on_task_start: Callable[[Task], None] | None = None,
        on_task_progress: Callable[[Task, int, int], None] | None = None,
        on_task_done: Callable[[TaskResult], None] | None = None,
        on_log: Callable[[str], None] | None = None,
        on_queue_progress: Callable[[int, int], None] | None = None,
    ):
        self.config = config
        self.state = state
        self.cookies = list(cookies or [])
        # 让状态库按当前下载目录解析记录里的相对路径
        self.state.set_output_dir(config.output_dir)
        self.on_task_start = on_task_start or (lambda task: None)
        self.on_task_progress = on_task_progress or (lambda task, done, total: None)
        self.on_task_done = on_task_done or (lambda result: None)
        self.on_log = on_log or (lambda message: None)
        self.on_queue_progress = on_queue_progress or (lambda done, total: None)

        self._local = threading.local()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._done_count = 0
        self._total_count = 0
        self._last_report: dict[str, float] = {}
        self._bytes_total = 0
        self._bytes_done = 0

    # ------------------------------------------------------------------ 控制
    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    # ------------------------------------------------------------------ 会话
    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": USER_AGENT,
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://www.patreon.com/",
                }
            )
            if self.cookies:
                apply_to_session(session, self.cookies)
            proxy = (self.config.proxy or "").strip()
            if proxy:
                session.proxies.update({"http": proxy, "https": proxy})
            self._local.session = session
        return session

    # ------------------------------------------------------------ 队列执行
    def run(self, tasks: list[Task]) -> list[TaskResult]:
        results: list[TaskResult] = []
        self._total_count = len(tasks)
        self._done_count = 0
        self._bytes_total = sum(int(t.item.size or 0) for t in tasks)
        self._bytes_done = 0

        if not tasks:
            return results

        workers = max(1, min(int(self.config.max_workers or 4), 16, len(tasks)))
        self.on_log(f"开始下载 {len(tasks)} 个文件，并发 {workers}")

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dl") as pool:
            future_map = {pool.submit(self._worker, task): task for task in tasks}
            for future in as_completed(future_map):
                task = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001
                    result = TaskResult(task=task, status=STATUS_FAILED, error=str(exc))
                results.append(result)
                with self._lock:
                    self._done_count += 1
                    done = self._done_count
                self.on_task_done(result)
                self.on_queue_progress(done, self._total_count)
                if result.status == STATUS_DONE:
                    self.state.save()

        self.state.save(force=True)
        return results

    def _worker(self, task: Task) -> TaskResult:
        started = time.monotonic()
        if self._stop.is_set():
            return TaskResult(task=task, status=STATUS_STOPPED, error="已停止")

        key = task.key
        campaign_id = task.campaign_id

        # 增量：已下载且大小一致则跳过
        if self.config.skip_existing:
            record = self.state.record_of(campaign_id, key)
            if self.state.is_downloaded(campaign_id, key, task.item.size, self.config.verify_size):
                path = self.state.resolve_path(campaign_id, key) or (record or {}).get("path", "")
                self.on_log(f"跳过（已存在）：{task.filename}")
                return TaskResult(task=task, status=STATUS_SKIPPED, path=path,
                                  size=os.path.getsize(path) if path and os.path.exists(path) else 0,
                                  seconds=time.monotonic() - started)
            if not record and os.path.exists(task.dest_path):
                existing = os.path.getsize(task.dest_path)
                expected = task.item.size
                if expected is None or not self.config.verify_size or existing == expected:
                    self.state.mark(campaign_id, key, task.dest_path, existing, task.item.kind, task.item.url)
                    self.on_log(f"跳过（文件已在目标位置）：{task.filename}")
                    return TaskResult(task=task, status=STATUS_SKIPPED, path=task.dest_path,
                                      size=existing, seconds=time.monotonic() - started)

        self.on_task_start(task)

        try:
            os.makedirs(task.dest_dir, exist_ok=True)
        except OSError as exc:
            return TaskResult(task=task, status=STATUS_FAILED, error=f"无法创建目录：{exc}",
                              seconds=time.monotonic() - started)

        if task.item.kind == "embed" and self.config.use_yt_dlp:
            try:
                path, size = self._download_with_ytdlp(task)
                self.state.mark(campaign_id, key, path, size, task.item.kind, task.item.url)
                return TaskResult(task=task, status=STATUS_DONE, path=path, size=size,
                                  seconds=time.monotonic() - started)
            except Exception as exc:  # noqa: BLE001
                return TaskResult(task=task, status=STATUS_FAILED, error=str(exc),
                                  seconds=time.monotonic() - started)

        urls = [task.item.url] + [u for u in (task.item.extra.get("fallbacks") or []) if u]
        last_error = ""
        index = 0
        attempts = 0
        while index < len(urls):
            url = urls[index]
            index += 1
            attempts += 1
            if self._stop.is_set():
                return TaskResult(task=task, status=STATUS_STOPPED, error="已停止",
                                  seconds=time.monotonic() - started)
            try:
                path, size, resumed = self._fetch_to_file(task, url)
                self.state.mark(campaign_id, key, path, size, task.item.kind, url)
                if url != task.item.url:
                    self.on_log(f"改用备用地址成功：{os.path.basename(path)}")
                return TaskResult(task=task, status=STATUS_DONE, path=path, size=size,
                                  seconds=time.monotonic() - started, resumed=resumed,
                                  attempts=attempts)
            except StopRequested:
                return TaskResult(task=task, status=STATUS_STOPPED, error="已停止",
                                  seconds=time.monotonic() - started)
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                self.on_log(f"下载失败（{attempts}/{len(urls)}）：{task.filename} — {last_error}")
                if "mux.com" not in url:
                    continue
                if "403" in last_error:
                    # 令牌不对（例如误用了封面图令牌），换清晰度也没用
                    self.on_log("mux 拒绝访问，跳过其余清晰度候选")
                    break
                if "404" in last_error and url.lower().split("?")[0].endswith(".mp4"):
                    # 受保护播放没有静态 MP4，直接跳到 HLS 播放列表
                    hls_index = next(
                        (i for i in range(index, len(urls)) if ".m3u8" in urls[i].lower()),
                        None,
                    )
                    if hls_index is not None:
                        self.on_log("该视频没有静态 MP4（受保护的 mux 播放），改用 HLS 分片下载")
                        index = hls_index
                        continue
                    break

        return TaskResult(task=task, status=STATUS_FAILED, error=last_error,
                          seconds=time.monotonic() - started, attempts=attempts)

    # ------------------------------------------------------------ 实际下载
    def _fetch_to_file(self, task: Task, url: str) -> tuple[str, int, bool]:
        session = self._session()
        is_hls = any(hint in url.lower() for hint in HLS_HINTS)

        part_name = task.filename + ".part"
        part_path = os.path.join(task.dest_dir, part_name)
        resume_from = 0
        if not is_hls and os.path.exists(part_path):
            resume_from = os.path.getsize(part_path)

        self.on_task_progress(task, 0, int(task.item.size or 0))

        if is_hls:
            used_init_segment = self._download_hls(task, url, part_path)
            if used_init_segment:
                # mux 的 fMP4：init 分片 + 媒体分片拼起来本身就是合法 mp4
                final_path = self._finalize_path(task, None, url)
            else:
                # MPEG-TS 分片：优先用 ffmpeg 无损封装成 mp4，否则老实叫 .ts
                desired = self._finalize_path(task, None, url)
                remuxed = self._remux_ts_to_mp4(task, part_path, desired)
                if remuxed:
                    final_path = self._unique_or_replace(desired, task)
                    try:
                        os.replace(remuxed, final_path)
                    except OSError:
                        shutil.move(remuxed, final_path)
                    try:
                        os.remove(part_path)      # 清掉中间产物
                    except OSError:
                        pass
                    size = os.path.getsize(final_path)
                    self.on_task_progress(task, size, size)
                    return final_path, size, False
                final_path = self._finalize_path(task, None, url, force_ext=".ts")
            size = os.path.getsize(part_path)
        else:
            headers = {"Referer": task.item.referer or "https://www.patreon.com/"}
            if resume_from:
                headers["Range"] = f"bytes={resume_from}-"
            try:
                response = session.get(
                    url,
                    headers=headers,
                    stream=True,
                    timeout=(20, max(30, int(self.config.timeout))),
                    allow_redirects=True,
                )
            except requests.RequestException as exc:
                raise DownloadError(f"网络错误：{exc}") from exc

            with response:
                if response.status_code == 416:
                    # 本地文件已经和远端一样大
                    final_path = self._finalize_path(task, response.headers.get("Content-Type"), url)
                    size = os.path.getsize(part_path)
                    os.replace(part_path, final_path)
                    return final_path, size, True
                if response.status_code not in (200, 206):
                    raise DownloadError(f"HTTP {response.status_code}")
                if response.status_code == 200 and resume_from:
                    resume_from = 0  # 服务端忽略了 Range，从头写
                content_type = response.headers.get("Content-Type", "")
                final_path = self._finalize_path(task, content_type, url)
                mode = "ab" if resume_from else "wb"
                written = resume_from
                with open(self._write_path(part_path), mode) as handle:
                    for chunk in response.iter_content(chunk_size=self.config.chunk_size):
                        if self._stop.is_set():
                            raise StopRequested()
                        if not chunk:
                            continue
                        handle.write(chunk)
                        written += len(chunk)
                        self._report_progress(task, written)
                size = written

        if size <= 0:
            raise DownloadError("下载内容为空")

        final_path = self._unique_or_replace(final_path, task)
        try:
            os.replace(part_path, final_path)
        except OSError:
            shutil.move(part_path, final_path)
        self.on_task_progress(task, size, size)
        return final_path, size, bool(resume_from)

    @staticmethod
    def _write_path(path: str) -> str:
        """超过 MAX_PATH 时加长路径前缀，保证大目录下也能写入。"""
        if os.name != "nt":
            return path
        absolute = os.path.abspath(path)
        if len(absolute) >= 240 and not absolute.startswith("\\\\?\\"):
            return "\\\\?\\" + absolute
        return path

    def _finalize_path(self, task: Task, content_type: str | None, url: str,
                       force_ext: str | None = None) -> str:
        """根据 Content-Type 修正扩展名，得到最终目标路径。"""
        filename = task.filename
        stem, ext = os.path.splitext(filename)
        if force_ext:
            ext = force_ext
        elif content_type:
            ctype = content_type.split(";")[0].strip().lower()
            if ctype in ("application/x-mpegurl", "application/vnd.apple.mpegurl"):
                ext = ".mp4"
            elif ctype.startswith("image/") or ctype.startswith("video/") or ctype.startswith("audio/"):
                better = guess_extension(ctype, url, default="")
                if better and (not ext or ext.lstrip(".").lower() != better):
                    ext = "." + better
            elif ctype == "application/octet-stream" and not ext:
                ext = ""
        elif not ext:
            guess = guess_extension(None, url, default="")
            if guess:
                ext = "." + guess
        return os.path.join(task.dest_dir, stem + ext)

    def _unique_or_replace(self, final_path: str, task: Task) -> str:
        """目标已存在且不是本次的 .part，则换个名字避免覆盖。"""
        if not os.path.exists(final_path):
            return final_path
        # 用「当前能解析到的真实路径」比较：下载目录搬过家也算同一个文件
        recorded = self.state.resolve_path(task.campaign_id, task.key)
        if recorded and os.path.normcase(recorded) == os.path.normcase(final_path):
            return final_path
        if self.config.skip_existing and not self.config.verify_size:
            return final_path
        stem, ext = os.path.splitext(os.path.basename(final_path))
        counter = 2
        while True:
            candidate = os.path.join(task.dest_dir, f"{stem} ({counter}){ext}")
            if not os.path.exists(candidate):
                return candidate
            counter += 1

    def _report_progress(self, task: Task, written: int) -> None:
        key = task.key
        now = time.monotonic()
        last = self._last_report.get(key, 0.0)
        if now - last < 0.2:
            return
        self._last_report[key] = now
        expected = int(task.item.size or 0)
        self.on_task_progress(task, written, expected or written)

    # ------------------------------------------------------------------ HLS
    def _download_hls(self, task: Task, url: str, part_path: str) -> bool:
        """m3u8 下载：主播放列表 -> 最高码率 -> 顺序拼接分片。

        返回是否用到了 ``EXT-X-MAP`` 初始化分片（是则拼接结果就是 fMP4/mp4，
        否则是 MPEG-TS，扩展名需要相应调整）。
        """
        session = self._session()
        referer = task.item.referer or "https://www.patreon.com/"

        def get_text(target: str) -> str:
            response = session.get(target, headers={"Referer": referer},
                                   timeout=max(30, int(self.config.timeout)))
            if response.status_code != 200:
                raise DownloadError(f"HTTP {response.status_code} @ {target}")
            return response.text

        text = get_text(url)
        if "#EXT-X-STREAM-INF" in text:
            best_uri, best_bw = None, -1
            lines = [ln.strip() for ln in text.splitlines()]
            for idx, line in enumerate(lines):
                if line.startswith("#EXT-X-STREAM-INF"):
                    match = re.search(r"BANDWIDTH=(\d+)", line)
                    bandwidth = int(match.group(1)) if match else 0
                    for follow in lines[idx + 1:]:
                        if follow and not follow.startswith("#"):
                            if bandwidth > best_bw:
                                best_bw, best_uri = bandwidth, follow
                            break
            if not best_uri:
                raise DownloadError("无法从主播放列表中选择清晰度")
            url = urljoin(url, best_uri)
            text = get_text(url)

        init_segment = None
        segments: list[str] = []
        byte_range: str | None = None
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("#EXT-X-MAP"):
                match = re.search(r'URI="([^"]+)"', line)
                if match:
                    init_segment = urljoin(url, match.group(1))
            elif line.startswith("#EXT-X-BYTERANGE"):
                # 分片用字节范围表示，需要连同 Range 头一起请求
                byte_range = line.split(":", 1)[1].strip() if ":" in line else None
            elif not line.startswith("#"):
                segments.append((urljoin(url, line), byte_range))
                byte_range = None

        if not segments:
            raise DownloadError("播放列表里没有分片")

        self.on_log(f"HLS 分片下载：{len(segments)} 段")
        total = 0
        with open(self._write_path(part_path), "wb") as handle:
            if init_segment:
                response = session.get(init_segment, headers={"Referer": referer},
                                       timeout=max(30, int(self.config.timeout)))
                if response.status_code != 200:
                    raise DownloadError(f"初始化分片返回 HTTP {response.status_code}")
                handle.write(response.content)
                total += len(response.content)
            for index, (segment, ranges) in enumerate(segments, start=1):
                if self._stop.is_set():
                    raise StopRequested()
                headers = {"Referer": referer}
                if ranges:
                    headers["Range"] = f"bytes={ranges.replace('@', '-')}"
                response = session.get(segment, headers=headers,
                                       timeout=max(30, int(self.config.timeout)))
                if response.status_code not in (200, 206):
                    raise DownloadError(f"分片 {index} 返回 HTTP {response.status_code}")
                handle.write(response.content)
                total += len(response.content)
                self.on_task_progress(task, total, int(task.item.size or 0) or total)
                if index % 20 == 0 or index == len(segments):
                    self.on_log(f"HLS 进度 {index}/{len(segments)}")
        return init_segment is not None

    # ------------------------------------------------------- ffmpeg 无损封装
    def _ffmpeg_executable(self) -> str:
        """按设置 -> PATH 的顺序找 ffmpeg。"""
        configured = (self.config.ffmpeg_path or "").strip()
        if configured:
            if os.path.isfile(configured):
                return configured
            found = shutil.which(configured)
            if found:
                return found
        return shutil.which("ffmpeg") or ""

    def _remux_ts_to_mp4(self, task: Task, ts_path: str, dest_path: str) -> str | None:
        """把拼接好的 MPEG-TS 无损重新封装成 MP4（不重新编码，很快）。

        成功时返回**临时**输出文件路径（由调用方改名到 ``dest_path``）；
        没有 ffmpeg、或 ffmpeg 报错时返回 ``None``，调用方会保留 .ts 文件。
        """
        ffmpeg = self._ffmpeg_executable()
        if not ffmpeg:
            self.on_log("未找到 ffmpeg，保留 MPEG-TS 格式（.ts）；"
                        "装上 ffmpeg 后会自动封装成 .mp4")
            return None
        temp_out = os.path.join(
            task.dest_dir, ".patreon-remux-" + os.path.basename(dest_path)
        )
        command = [
            ffmpeg, "-y", "-loglevel", "error", "-i", ts_path,
            "-c", "copy", "-movflags", "+faststart", "-f", "mp4", temp_out,
        ]
        self.on_log("正在用 ffmpeg 无损封装为 MP4 …")
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True,
                timeout=max(900, int(self.config.timeout) * 30), check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.on_log(f"ffmpeg 调用失败，保留 .ts：{exc}")
            return None
        if completed.returncode != 0 or not os.path.isfile(temp_out) \
                or os.path.getsize(temp_out) == 0:
            detail = (completed.stderr or "").strip().replace("\n", " ")[:160]
            self.on_log(f"ffmpeg 封装失败，保留 .ts：{detail}")
            try:
                os.remove(temp_out)
            except OSError:
                pass
            return None
        return temp_out

    # -------------------------------------------------------------- yt-dlp
    def _download_with_ytdlp(self, task: Task) -> tuple[str, int]:
        """用 yt-dlp 下载 YouTube / Vimeo 等外链视频。"""
        executable = (self.config.yt_dlp_path or "yt-dlp").strip()
        if not shutil.which(executable) and not os.path.exists(executable):
            raise DownloadError("未找到 yt-dlp，请在设置里填写可执行文件路径")

        template = os.path.join(task.dest_dir, task.filename + ".%(ext)s")
        command = [
            executable, "--no-playlist", "--no-warnings",
            "--retries", str(max(1, self.config.retries)),
            "-o", template, task.item.url,
        ]
        if self.config.proxy:
            command += ["--proxy", self.config.proxy]
        if self.config.ffmpeg_path:
            command += ["--ffmpeg-location", self.config.ffmpeg_path]

        self.on_log(f"yt-dlp 下载：{task.item.url}")
        completed = subprocess.run(
            command, capture_output=True, text=True,
            timeout=max(600, int(self.config.timeout) * 30), check=False,
        )
        if completed.returncode != 0:
            raise DownloadError(f"yt-dlp 失败：{(completed.stderr or '').strip()[:200]}")

        for name in os.listdir(task.dest_dir):
            if name.startswith(os.path.basename(task.filename)):
                path = os.path.join(task.dest_dir, name)
                if os.path.isfile(path):
                    return path, os.path.getsize(path)
        raise DownloadError("yt-dlp 未产出文件")


# ------------------------------------------------------------------ 任务构建


def build_tasks(
    config: AppConfig,
    campaign: Campaign,
    posts: list[PostItem],
    state: StateStore,
    *,
    only_posts: set[str] | None = None,
    on_log: Callable[[str], None] | None = None,
) -> list[Task]:
    """把作品列表展开成下载任务（已过滤内容类型）。"""
    from .extract import filter_by_kind
    from .metadata import read_post_owner
    from .naming import creator_directory, media_filename, post_directory, section_directory

    log = on_log or (lambda message: None)
    tasks: list[Task] = []
    os.makedirs(creator_directory(config, campaign), exist_ok=True)

    for post in posts:
        if only_posts is not None and post.id not in only_posts:
            continue
        media = filter_by_kind(post.media, config)
        if not media:
            continue
        directory = post_directory(config, campaign, post)
        # 目录被另一篇作品占用了（同名同日期），加作品 ID 区分
        if config.group_by_post and os.path.isdir(directory):
            owner = read_post_owner(directory)
            if owner and owner != post.id:
                directory = post_directory(config, campaign, post, existing_owner=owner)

        # 帖子内部按段落分文件夹（新版富文本作品，一篇里塞很多视频）
        sections = post.distinct_sections
        split = bool(config.split_sections) and len(sections) >= 2
        section_order = {title: pos for pos, title in enumerate(sections, start=1)}
        section_dirs: dict[str, str] = {}
        counters: dict[str, int] = {}

        for item in media:
            folder = directory
            if split and item.section:
                if item.section not in section_dirs:
                    section_dirs[item.section] = section_directory(
                        config, campaign, post, item.section, section_order[item.section]
                    )
                folder = os.path.join(directory, section_dirs[item.section])
            index = counters.get(folder, 0) + 1
            counters[folder] = index
            filename = media_filename(config, campaign, post, item, index)
            tasks.append(
                Task(item=item, post=post, campaign=campaign,
                     dest_dir=folder, filename=filename, index=index)
            )
    log(f"已生成 {len(tasks)} 个下载任务")
    return tasks


def summarize(results: Iterable[TaskResult]) -> dict[str, Any]:
    """汇总一批下载结果，用于界面显示。"""
    done = skipped = failed = stopped = 0
    total_bytes = 0
    errors: list[str] = []
    for result in results:
        if result.status == STATUS_DONE:
            done += 1
            total_bytes += result.size
        elif result.status == STATUS_SKIPPED:
            skipped += 1
            total_bytes += result.size
        elif result.status == STATUS_STOPPED:
            stopped += 1
        else:
            failed += 1
            if result.error:
                errors.append(f"{result.task.filename}: {result.error}")
    return {
        "done": done, "skipped": skipped, "failed": failed, "stopped": stopped,
        "bytes": total_bytes, "size_text": human_size(total_bytes), "errors": errors,
    }
