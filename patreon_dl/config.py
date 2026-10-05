"""配置：应用路径、用户设置读写。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any


def _is_writable(directory: Path) -> bool:
    """目录能不能写（Windows 上 os.access 对目录不可靠，直接试写一个文件）。"""
    probe = directory / ".dsh-write-probe"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _resolve_app_dir() -> Path:
    """确定数据目录（data/）放在哪里。

    * 设了环境变量 ``PATREON_DL_HOME`` → 用它；
    * 打包成 exe（PyInstaller 等）→ 放在 **exe 旁边**，
      而不是临时解包目录（``sys._MEIPASS``），否则配置和登录状态每次都丢；
    * exe 所在目录不可写（例如装在 Program Files）→ 退回
      ``%LOCALAPPDATA%\\PatreonDownloader``；
    * 源码方式运行 → 项目根目录。
    """
    override = os.environ.get("PATREON_DL_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()

    if getattr(sys, "frozen", False):
        beside = Path(sys.executable).resolve().parent
        if _is_writable(beside):
            return beside
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "PatreonDownloader"

    return Path(__file__).resolve().parent.parent


APP_DIR = _resolve_app_dir()
DATA_DIR = APP_DIR / "data"
WEB_PROFILE_DIR = DATA_DIR / "webprofile"
LOG_DIR = DATA_DIR / "logs"

CONFIG_FILE = DATA_DIR / "config.json"
COOKIE_FILE = DATA_DIR / "cookies.json"
STATE_FILE = DATA_DIR / "state.json"

DEFAULT_OUTPUT_DIR = str(Path.home() / "PatreonDownloads")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def ensure_dirs() -> None:
    """确保所有数据目录存在。"""
    for path in (DATA_DIR, WEB_PROFILE_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)


@dataclass
class AppConfig:
    """用户可调设置。所有字段都会写入 data/config.json。"""

    # 基本
    output_dir: str = DEFAULT_OUTPUT_DIR
    max_workers: int = 4
    request_delay: float = 0.35          # 每个请求之间的间隔（秒），降低被限流的概率
    page_size: int = 25                  # 每页抓取的作品数量（Patreon 上限约 100）
    max_posts: int = 0                   # 0 表示不限制
    timeout: int = 60
    retries: int = 3
    chunk_size: int = 512 * 1024
    proxy: str = ""                      # 例如 http://127.0.0.1:7890

    # 下载内容
    download_images: bool = True
    download_videos: bool = True
    download_audio: bool = True
    download_attachments: bool = True
    download_previews: bool = False      # 是否下载视频的 30 秒预览片段
    download_thumbnails: bool = False    # 是否把视频封面图也当作图片下载
    fetch_inline_media: bool = True      # 抓取正文富文本里内嵌的媒体（需额外请求）
    image_quality: str = "original"      # original | large | medium
    prefer_mux_full: bool = True         # 视频优先取 mux 完整版 (highest.mp4)
    max_video_height: int = 0            # 0 = 最高画质；否则用 mux 对应分辨率

    # 命名与目录
    folder_template: str = "{creator}"
    name_template: str = "{date}_{title}"     # 作品子目录名
    file_template: str = "{index:02d}_{name}" # 文件名模板
    group_by_post: bool = True           # 每个作品单独一个子目录
    split_sections: bool = False         # 帖子内含多个部分时，每部分单独子目录
    section_template: str = "{index:02d}_{title}"   # 部分子目录名模板
    write_metadata: bool = True          # 保存作品正文与媒体清单 (JSON)
    write_post_text: bool = True         # 保存作品正文为 txt

    # 行为
    skip_existing: bool = True           # 增量：跳过已下载
    verify_size: bool = True             # 已存在但大小不符时重新下载
    overwrite_partial: bool = True
    open_folder_when_done: bool = False
    use_system_browser: bool = True      # 登录时允许在内置浏览器里打开系统下载等

    # 外链视频（YouTube / Vimeo 等）交给 yt-dlp
    use_yt_dlp: bool = False
    yt_dlp_path: str = "yt-dlp"
    ffmpeg_path: str = ""

    # 界面
    theme: str = "dark"                  # dark | light
    log_level: str = "INFO"

    # 记录上次使用的创作者，方便续用
    recent_creators: list[str] = field(default_factory=list)

    # ---------------------------------------------------------------- 读写
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AppConfig":
        known = {f.name: f for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        for key, value in (raw or {}).items():
            spec = known.get(key)
            if spec is None:
                continue
            try:
                if spec.type in ("int", int) or spec.type == "int":
                    kwargs[key] = int(value)
                elif spec.type in ("float", float) or spec.type == "float":
                    kwargs[key] = float(value)
                elif spec.type in ("bool", bool) or spec.type == "bool":
                    kwargs[key] = bool(value)
                else:
                    kwargs[key] = value
            except (TypeError, ValueError):
                continue
        return cls(**kwargs)

    def save(self, path: Path | None = None) -> None:
        target = Path(path) if path else CONFIG_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(target, self.to_dict())

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        source = Path(path) if path else CONFIG_FILE
        if not source.exists():
            return cls()
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        return cls.from_dict(raw)

    def remember_creator(self, value: str, keep: int = 12) -> None:
        value = (value or "").strip()
        if not value:
            return
        items = [c for c in self.recent_creators if c.lower() != value.lower()]
        items.insert(0, value)
        self.recent_creators = items[:keep]


def _atomic_write_json(path: Path, payload: Any) -> None:
    """先写临时文件再替换，避免中途崩溃损坏配置。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_json(path: Path, payload: Any) -> None:
    _atomic_write_json(path, payload)
