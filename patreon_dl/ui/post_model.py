"""作品列表 / 下载队列的表格模型。"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal

from ..models import MediaItem, PostItem
from ..util import human_size

POST_TYPE_LABELS = {
    "image_file": "图片集",
    "image": "图片",
    "video_embed": "外链视频",
    "video_external_file": "视频",
    "video_file": "视频",
    "audio_file": "音频",
    "text_only": "纯文字",
    "attachment": "附件",
    "attachment_file": "附件",
    "link": "链接",
    "poll": "投票",
    "livestream": "直播",
}

STATUS_COLORS = {
    "ok": "#4ec98a",
    "partial": "#f0b34a",
    "none": "#98a1b0",
    "locked": "#ff6b6b",
}


class PostTableModel(QAbstractTableModel):
    """作品列表：第一列是勾选框，最后一列显示下载状态。"""

    HEADERS = ["", "标题", "发布时间", "类型", "内容", "数量", "状态", "链接"]

    selection_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all: list[PostItem] = []
        self._rows: list[PostItem] = []
        self._checked: set[str] = set()
        self._index_of: dict[str, int] = {}
        self.status_provider: Callable[[PostItem], tuple[str, str]] | None = None
        self._sort_column = 2
        self._sort_order = Qt.SortOrder.DescendingOrder

    # ------------------------------------------------------------ 数据管理
    def set_posts(self, posts: Iterable[PostItem]) -> None:
        self.beginResetModel()
        merged: dict[str, PostItem] = {p.id: p for p in self._all}
        for post in posts:
            merged[post.id] = post
        self._all = list(merged.values())
        self._reindex()
        self._rebuild_rows()
        self.endResetModel()
        self.selection_changed.emit()

    def append_posts(self, posts: Iterable[PostItem]) -> None:
        posts = [p for p in posts if p is not None]
        if not posts:
            return
        # 直接用整表重置：批次很小，重置比增量插入更不容易出现视图与行的错位
        self.beginResetModel()
        for post in posts:
            if post.id not in self._index_of:
                self._index_of[post.id] = len(self._all)
                self._all.append(post)
        self._rows = list(self._all)
        self.endResetModel()

    def clear(self) -> None:
        self.beginResetModel()
        self._all = []
        self._rows = []
        self._index_of = {}
        self.endResetModel()
        self.selection_changed.emit()

    def _reindex(self) -> None:
        self._index_of = {post.id: i for i, post in enumerate(self._all)}

    def apply_filter(self, keyword: str = "", kinds: set[str] | None = None,
                     only_pending: bool = False,
                     collection_ids: set[str] | None = None) -> None:
        keyword = (keyword or "").strip().lower()
        self.beginResetModel()
        rows: list[PostItem] = []
        for post in self._all:
            if keyword and keyword not in post.safe_title.lower() and keyword not in post.id:
                continue
            if kinds and post.post_type not in kinds:
                continue
            if collection_ids is not None and post.id not in collection_ids:
                continue
            if only_pending and self._is_complete(post):
                continue
            rows.append(post)
        self._rows = rows
        self.endResetModel()
        self.selection_changed.emit()

    def _is_complete(self, post: PostItem) -> bool:
        if not self.status_provider:
            return False
        text, _color = self.status_provider(post)
        return "已下载" in text or "完成" in text

    def _rebuild_rows(self) -> None:
        self.layoutAboutToBeChanged.emit()
        self._rows = list(self._all)
        self.layoutChanged.emit()

    def post_at(self, row: int) -> PostItem | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def all_posts(self) -> list[PostItem]:
        return list(self._all)

    def visible_posts(self) -> list[PostItem]:
        return list(self._rows)

    # ------------------------------------------------------------- 勾选
    def checked_posts(self) -> list[PostItem]:
        return [post for post in self._all if post.id in self._checked]

    def checked_ids(self) -> set[str]:
        return set(self._checked)

    def set_checked(self, post_id: str, checked: bool) -> None:
        if checked:
            self._checked.add(post_id)
        else:
            self._checked.discard(post_id)
        self.selection_changed.emit()

    def set_all_checked(self, checked: bool, visible_only: bool = True) -> None:
        targets = self._rows if visible_only else self._all
        for post in targets:
            if checked:
                self._checked.add(post.id)
            else:
                self._checked.discard(post.id)
        if self._rows:
            top = self.index(0, 0)
            bottom = self.index(len(self._rows) - 1, 0)
            self.dataChanged.emit(top, bottom, [Qt.ItemDataRole.CheckStateRole])
        self.selection_changed.emit()

    def checked_media_count(self) -> int:
        return sum(len(self._filtered_media(post)) for post in self.checked_posts())

    def _filtered_media(self, post: PostItem) -> list[MediaItem]:
        return list(post.media)

    # ------------------------------------------------------- Qt 模型接口
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation,  # noqa: N802
                   role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation != Qt.Orientation.Horizontal:
            return section + 1
        if role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        if role == Qt.ItemDataRole.ToolTipRole and section == 0:
            return "勾选要下载的作品"
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == 0:
            return base | Qt.ItemFlag.ItemIsUserCheckable
        return base

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        post = self._rows[index.row()]
        column = index.column()

        if role == Qt.ItemDataRole.CheckStateRole and column == 0:
            return (Qt.CheckState.Checked if post.id in self._checked
                    else Qt.CheckState.Unchecked)

        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            if column == 0:
                return None
            if column == 1:
                title = post.safe_title
                if role == Qt.ItemDataRole.ToolTipRole:
                    return f"{title}\n{post.url}"
                if not post.can_view:
                    title = "🔒 " + title
                return title
            if column == 2:
                return post.date_text
            if column == 3:
                return POST_TYPE_LABELS.get(post.post_type, post.post_type or "-")
            if column == 4:
                return post.count_text()
            if column == 5:
                total = post.total_size()
                return f"{post.media_count}" + (f" · {human_size(total)}" if total else "")
            if column == 6:
                if self.status_provider:
                    return self.status_provider(post)[0]
                return "-"
            if column == 7:
                return post.url
            return None

        if role == Qt.ItemDataRole.ForegroundRole and column == 6 and self.status_provider:
            from PySide6.QtGui import QColor

            return QColor(self.status_provider(post)[1])
        if role == Qt.ItemDataRole.TextAlignmentRole and column in (2, 5):
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.UserRole:
            return post
        return None

    def setData(self, index: QModelIndex, value: Any,  # noqa: N802
                role: int = Qt.ItemDataRole.EditRole) -> bool:
        if not index.isValid() or index.column() != 0:
            return False
        if role == Qt.ItemDataRole.CheckStateRole:
            post = self._rows[index.row()]
            checked = Qt.CheckState(value) == Qt.CheckState.Checked
            self.set_checked(post.id, checked)
            self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
            return True
        return False

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        if column == 0 or not self._rows:
            return
        self._sort_column = column
        self._sort_order = order
        reverse = order == Qt.SortOrder.DescendingOrder
        keys: dict[int, Any] = {
            1: lambda p: p.safe_title.lower(),
            2: lambda p: p.published_at or "",
            3: lambda p: p.post_type or "",
            4: lambda p: p.media_count,
            5: lambda p: p.media_count,
            6: lambda p: p.id,
            7: lambda p: p.url or "",
        }
        key = keys.get(column)
        if not key:
            return
        self.layoutAboutToBeChanged.emit()
        self._rows.sort(key=key, reverse=reverse)
        self.layoutChanged.emit()


class MediaTableModel(QAbstractTableModel):
    """选中作品的媒体明细。"""

    HEADERS = ["文件名", "类型", "格式", "大小", "时长", "分辨率"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[MediaItem] = []

    def set_media(self, items: Iterable[MediaItem]) -> None:
        self.beginResetModel()
        self._items = list(items)
        self.endResetModel()

    def item_at(self, row: int) -> MediaItem | None:
        if 0 <= row < len(self._items):
            return self._items[row]
        return None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._items)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation,  # noqa: N802
                   role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        item = self._items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            if index.column() == 0:
                return item.filename
            if index.column() == 1:
                return item.kind_label
            if index.column() == 2:
                return (item.mimetype or "-").replace("application/", "")
            if index.column() == 3:
                size = item.display_size
                return human_size(size) if size else "-"
            if index.column() == 4:
                from ..util import human_duration

                return human_duration(item.duration) if item.duration else "-"
            if index.column() == 5:
                if item.width and item.height:
                    return f"{item.width}x{item.height}"
                return "-"
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{item.filename}\n{item.url}"
        return None


class QueueTableModel(QAbstractTableModel):
    """下载队列。"""

    HEADERS = ["文件", "作品", "类型", "大小", "进度", "状态"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[dict[str, Any]] = []
        self._index: dict[str, int] = {}

    def clear(self) -> None:
        self.beginResetModel()
        self._rows = []
        self._index = {}
        self.endResetModel()

    def add_task(self, task) -> None:
        row = {
            "key": task.key,
            "file": task.filename,
            "post": task.post.safe_title,
            "kind": task.item.kind_label,
            "size": int(task.item.size or 0),
            "done": 0,
            "status": "等待中",
        }
        self._index[task.key] = len(self._rows)
        self.beginInsertRows(QModelIndex(), len(self._rows), len(self._rows))
        self._rows.append(row)
        self.endInsertRows()

    def update_progress(self, task, done: int, total: int) -> None:
        row = self._index.get(task.key)
        if row is None:
            return
        self._rows[row]["done"] = done
        if total:
            self._rows[row]["size"] = total
        if self._rows[row]["status"] == "等待中":
            self._rows[row]["status"] = "下载中"
        left = self.index(row, 4)
        right = self.index(row, 5)
        self.dataChanged.emit(left, right)

    def set_status(self, task, status: str) -> None:
        row = self._index.get(task.key)
        if row is None:
            return
        self._rows[row]["status"] = status
        if status == "已完成":
            self._rows[row]["done"] = self._rows[row]["size"]
        left = self.index(row, 4)
        right = self.index(row, 5)
        self.dataChanged.emit(left, right)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation,  # noqa: N802
                   role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            column = index.column()
            if column == 0:
                return row["file"]
            if column == 1:
                return row["post"]
            if column == 2:
                return row["kind"]
            if column == 3:
                return human_size(row["size"]) if row["size"] else "-"
            if column == 4:
                done, total = row["done"], row["size"]
                if total:
                    return f"{done * 100 // total}%"
                return human_size(done) if done else "0%"
            if column == 5:
                return row["status"]
        if role == Qt.ItemDataRole.UserRole:
            return row
        return None
