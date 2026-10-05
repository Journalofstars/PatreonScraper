"""下载状态记录：支持增量同步（跳过已下载）。

**判断基准是「下载目录内的相对路径」，不是绝对路径。**
每条记录同时保存：

* ``rel``  —— 相对当前下载目录的路径，例如
  ``NSFW VTuber Roleplay Videos\\2026-10-03_标题\\01_封面.png``；
* ``path`` —— 当时的绝对路径，仅作参考与兜底。

所以只要把整个下载目录搬走（``G:/bb`` → ``E:/bb``）并在设置里改成新目录，
目录内的结构没变，就依然算「已下载」，不会重下。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .config import STATE_FILE
from .config import write_json


class StateStore:
    """轻量的 JSON 状态库，记录每个媒体是否已下载。"""

    def __init__(self, path: Path | None = None, output_dir: str = ""):
        self.path = Path(path) if path else STATE_FILE
        self.output_dir = str(output_dir or "")
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {"version": 1, "campaigns": {}}
        self._dirty = False
        self._last_save = 0.0
        self.load()

    # ------------------------------------------------------------------ 读写
    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(raw, dict):
            self._data = raw
            self._data.setdefault("version", 1)
            self._data.setdefault("campaigns", {})

    def save(self, force: bool = False) -> None:
        with self._lock:
            if not self._dirty and not force:
                return
            try:
                write_json(self.path, self._data)
                self._dirty = False
                self._last_save = time.monotonic()
            except OSError:
                pass

    def maybe_save(self, interval: float = 5.0) -> None:
        if self._dirty and (time.monotonic() - self._last_save) >= interval:
            self.save()

    def set_output_dir(self, output_dir: str) -> None:
        """更新当前下载目录；之后所有相对路径都按它解析。"""
        self.output_dir = str(output_dir or "")

    # ------------------------------------------------------------- 内部结构
    def _campaign(self, campaign_id: str) -> dict[str, Any]:
        campaigns = self._data.setdefault("campaigns", {})
        return campaigns.setdefault(str(campaign_id), {"media": {}, "posts": {}})

    # ------------------------------------------------------------- 路径解析
    @staticmethod
    def relative_path(path: str, root: str) -> str:
        """``path`` 相对 ``root`` 的路径；不在 ``root`` 下时返回空串。"""
        if not path or not root:
            return ""
        try:
            rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
        except (ValueError, OSError):
            return ""
        if not rel or rel == "." or rel.startswith("..") or os.path.isabs(rel):
            return ""
        return rel

    def _candidate_paths(self, record: dict[str, Any]) -> list[tuple[str, bool]]:
        """列出这条记录可能的真实文件位置 ``[(路径, 是否为猜测)]``。

        **只认当前下载目录内的位置**——这是「只考虑下载目录中的内容」这条规则。
        优先级从高到低：

        1. ``rel`` 相对当前下载目录解析；
        2. 记录的绝对路径，前提是它仍在当前下载目录内（老记录 / 目录没变）；
        3. 用绝对路径的尾部在当前下载目录里逐级找回
           （把 ``/bb/创作者/作品/文件.png`` 依次试
           ``<新目录>/创作者/作品/文件.png``、``<新目录>/作品/文件.png``…）。
           这属于猜测，必须大小吻合才认。
        """
        absolute = str(record.get("path") or "")
        root = self.output_dir

        # 没配置下载目录（例如独立使用状态库）时，退化成只信绝对路径
        if not root:
            return [(absolute, False)] if absolute else []

        candidates: list[tuple[str, bool]] = []
        seen: set[str] = set()

        def push(path: str, guess: bool) -> None:
            key = os.path.normcase(os.path.abspath(path))
            if key not in seen:
                seen.add(key)
                candidates.append((path, guess))

        rel = str(record.get("rel") or "")
        if rel:
            push(os.path.join(root, rel), False)

        if absolute:
            if self.relative_path(absolute, root):
                push(absolute, False)          # 本来就在下载目录里
            else:
                # 已换目录：用路径尾部在当前下载目录里找回来
                parts = Path(absolute).parts
                for start in range(1, len(parts)):
                    push(os.path.join(root, *parts[start:]), True)
        return candidates

    def resolve_path(self, campaign_id: str, key: str) -> str | None:
        """返回这条记录当前真实存在的文件路径；找不到返回 None。"""
        record = self.record_of(campaign_id, key)
        if not record:
            return None
        size = record.get("size")

        for candidate, is_guess in self._candidate_paths(record):
            if not os.path.isfile(candidate):
                continue
            if is_guess and size:
                # 猜测出来的位置必须大小完全吻合，避免同名的其它文件被误认
                try:
                    if os.path.getsize(candidate) != int(size):
                        continue
                except OSError:
                    continue
            self._heal(campaign_id, record, candidate)
            return candidate
        return None

    def _heal(self, campaign_id: str, record: dict[str, Any], found: str) -> None:
        """文件位置变了就悄悄更新记录，下次直接命中。"""
        rel_now = self.relative_path(found, self.output_dir) if self.output_dir else ""
        with self._lock:
            changed = False
            if os.path.normcase(str(record.get("path") or "")) != os.path.normcase(found):
                record["path"] = found
                changed = True
            if rel_now and record.get("rel") != rel_now:
                record["rel"] = rel_now
                changed = True
            if changed:
                self._dirty = True
        if changed:
            self.maybe_save()

    def reindex(self, output_dir: str | None = None) -> int:
        """给老记录补上 ``rel`` 字段（相对当前下载目录），返回补写条数。"""
        if output_dir is not None:
            self.set_output_dir(output_dir)
        count = 0
        with self._lock:
            for campaign in (self._data.get("campaigns") or {}).values():
                for record in (campaign.get("media") or {}).values():
                    if record.get("rel") or not isinstance(record, dict):
                        continue
                    rel = self.relative_path(str(record.get("path") or ""), self.output_dir)
                    if rel:
                        record["rel"] = rel
                        count += 1
            if count:
                self._dirty = True
        if count:
            self.save(force=True)
        return count

    # --------------------------------------------------------------- 查询
    def is_downloaded(self, campaign_id: str, key: str, size: int | None = None,
                      verify_size: bool = True) -> bool:
        if not self.record_of(campaign_id, key):
            return False
        path = self.resolve_path(campaign_id, key)
        if not path:
            return False
        if verify_size and size:
            try:
                if os.path.getsize(path) != int(size):
                    return False
            except OSError:
                return False
        return True

    def record_of(self, campaign_id: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            return self._campaign(campaign_id).get("media", {}).get(key)

    def post_record(self, campaign_id: str, post_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._campaign(campaign_id).get("posts", {}).get(str(post_id))

    def post_downloaded_count(self, campaign_id: str, post_id: str) -> int:
        prefix = f"{post_id}:"
        with self._lock:
            media = self._campaign(campaign_id).get("media", {})
            return sum(1 for key in media if key.startswith(prefix))

    # --------------------------------------------------------------- 写入
    def mark(self, campaign_id: str, key: str, path: str, size: int | None,
             kind: str = "", url: str = "") -> None:
        record: dict[str, Any] = {
            "path": str(path),
            "size": int(size) if size else None,
            "kind": kind,
            "url": url,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        rel = self.relative_path(str(path), self.output_dir)
        if rel:
            record["rel"] = rel
        with self._lock:
            self._campaign(campaign_id).setdefault("media", {})[key] = record
            self._dirty = True
        self.maybe_save()

    def mark_post(self, campaign_id: str, post_id: str, media_total: int, media_done: int,
                  message: str = "") -> None:
        with self._lock:
            self._campaign(campaign_id).setdefault("posts", {})[str(post_id)] = {
                "media_total": media_total,
                "media_done": media_done,
                "message": message,
                "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self._dirty = True
        self.maybe_save()

    def forget(self, campaign_id: str, key: str) -> None:
        with self._lock:
            self._campaign(campaign_id).setdefault("media", {}).pop(key, None)
            self._dirty = True
        self.maybe_save()

    def clear_campaign(self, campaign_id: str) -> None:
        with self._lock:
            self._data.setdefault("campaigns", {}).pop(str(campaign_id), None)
            self._dirty = True
        self.save(force=True)

    def stats(self, campaign_id: str) -> dict[str, int]:
        with self._lock:
            media = self._campaign(campaign_id).get("media", {})
            total_bytes = sum(int(r.get("size") or 0) for r in media.values())
            return {"count": len(media), "bytes": total_bytes}
