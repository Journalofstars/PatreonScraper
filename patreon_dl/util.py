"""通用小工具：路径清理、体积格式化、稳定的媒体标识。"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from urllib.parse import urlsplit

_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DOTS = re.compile(r"^[.\s]+|[.\s]+$")
_MULTI_SPACE = re.compile(r"\s{2,}")


def sanitize_filename(name: str, fallback: str = "untitled", max_length: int = 150) -> str:
    """把任意字符串变成在 Windows 上安全的文件名（不含目录部分）。"""
    if not name:
        return fallback
    name = unicodedata.normalize("NFC", str(name))
    name = _ILLEGAL.sub("_", name)
    name = _MULTI_SPACE.sub(" ", name)
    name = _DOTS.sub("", name)
    name = name.strip()
    if not name:
        return fallback

    stem, ext = os.path.splitext(name)
    if stem.upper() in _WINDOWS_RESERVED:
        stem = "_" + stem
    if len(stem) > max_length:
        # 尽量在词边界截断，别切出「Voiced Vt」这种半截词
        cut = stem[:max_length]
        space = max(cut.rfind(" "), cut.rfind("_"), cut.rfind("-"))
        if space >= max_length * 0.6:
            cut = cut[:space]
        stem = cut.rstrip()
    name = stem + ext
    return name or fallback


def sanitize_dirname(name: str, fallback: str = "untitled") -> str:
    """目录名清理，进一步去掉结尾的点（Windows 目录不允许）。"""
    cleaned = sanitize_filename(name, fallback, max_length=80)
    return cleaned.rstrip(". ") or fallback


def human_size(num: float | int | None) -> str:
    """把字节数格式化成易读字符串。"""
    if num is None:
        return "-"
    try:
        value = float(num)
    except (TypeError, ValueError):
        return "-"
    if value < 0:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(value)} {units[idx]}"
    return f"{value:.2f} {units[idx]}"


def human_duration(seconds: float | int | None) -> str:
    """秒 -> 时:分:秒。"""
    if not seconds:
        return "-"
    total = int(float(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def stable_url_key(url: str) -> str:
    """生成与签名参数无关的 URL 指纹。

    Patreon 的媒体地址带有 ``token-hash`` / ``token-time`` / ``token`` 等
    临时签名，重新抓取时会变化。这里只保留协议、主机与路径，保证同一条
    媒体在多次运行中得到同一个 key。
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()
    base = f"{parts.scheme}://{parts.netloc}{parts.path}"
    return hashlib.sha1(base.encode("utf-8", "replace")).hexdigest()


def guess_extension(mimetype: str | None, url: str = "", default: str = "bin") -> str:
    """根据 MIME 类型或 URL 推断扩展名。"""
    mapping = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
        "image/bmp": "bmp",
        "image/tiff": "tiff",
        "image/avif": "avif",
        "video/mp4": "mp4",
        "video/quicktime": "mov",
        "video/webm": "webm",
        "video/x-matroska": "mkv",
        "application/x-mpegurl": "mp4",
        "application/vnd.apple.mpegurl": "mp4",
        "video/mp2t": "ts",
        "audio/mpeg": "mp3",
        "audio/mp4": "m4a",
        "audio/aac": "aac",
        "audio/ogg": "ogg",
        "audio/wav": "wav",
        "audio/x-wav": "wav",
        "audio/flac": "flac",
        "application/pdf": "pdf",
        "application/zip": "zip",
        "application/x-zip-compressed": "zip",
        "application/x-7z-compressed": "7z",
        "application/x-rar-compressed": "rar",
        "image/vnd.adobe.photoshop": "psd",
        "application/postscript": "ai",
    }
    if mimetype:
        ext = mapping.get(mimetype.split(";")[0].strip().lower())
        if ext:
            return ext

    path = urlsplit(url).path if url else ""
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    if ext and len(ext) <= 6 and ext.isalnum():
        return ext
    return default


IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tif", "tiff", "avif", "heic"}
VIDEO_EXTS = {"mp4", "mov", "m4v", "webm", "mkv", "avi", "flv", "wmv", "mpg", "mpeg", "ts"}
AUDIO_EXTS = {"mp3", "m4a", "aac", "ogg", "oga", "wav", "flac", "opus"}
ARCHIVE_EXTS = {"zip", "rar", "7z", "tar", "gz", "bz2", "psd", "ai", "clip", "sai", "procreate", "pdf"}


def classify_extension(ext: str) -> str:
    """按扩展名粗略归类，用于在文件名缺失时决定类别。"""
    ext = (ext or "").lower().lstrip(".")
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in ARCHIVE_EXTS:
        return "attachment"
    return "other"


def unique_name(name: str, used: set[str]) -> str:
    """在 ``used`` 集合内保证文件名唯一，重复时追加 `` (2)`` 之类后缀。"""
    if name not in used:
        used.add(name)
        return name
    stem, ext = os.path.splitext(name)
    counter = 2
    while True:
        candidate = f"{stem} ({counter}){ext}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        counter += 1


def ensure_long_path(path: str) -> str:
    """为超过 MAX_PATH 的绝对路径加上 Windows 长路径前缀。"""
    if os.name != "nt":
        return path
    if path.startswith("\\\\?\\"):
        return path
    absolute = os.path.abspath(path)
    if len(absolute) >= 240:
        if absolute.startswith("\\\\"):
            return "\\\\?\\UNC\\" + absolute.lstrip("\\")
        return "\\\\?\\" + absolute
    return path


def safe_join(root: str, *parts: str) -> str:
    """拼接路径，并把每一段都做安全化处理，避免路径穿越。"""
    current = root
    for part in parts:
        if not part:
            continue
        for chunk in str(part).replace("\\", "/").split("/"):
            chunk = chunk.strip()
            if not chunk or chunk in (".", ".."):
                continue
            current = os.path.join(current, sanitize_filename(chunk, "item"))
    return current
