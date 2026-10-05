"""主窗口。"""

from __future__ import annotations

import os
from typing import Any

from PySide6.QtCore import QSize, Qt, QUrl, Slot
from PySide6.QtGui import QAction, QDesktopServices, QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QTableView,
    QTabWidget,
    QTextEdit,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import __app_name_cn__, __version__
from ..config import AppConfig, ensure_dirs
from ..cookies import CookieRecord, load_cookies
from ..downloader import TaskResult
from ..extract import ExtractOptions, filter_by_kind
from ..models import Campaign, Collection, PostItem
from ..patreon import PatreonError, parse_reference_all
from ..state import StateStore
from ..util import human_size
from .login_window import LoginDialog, browser_status, session_hint
from .post_model import MediaTableModel, PostTableModel, QueueTableModel, STATUS_COLORS
from .settings_dialog import SettingsDialog
from .theme import stylesheet
from .workers import (
    DownloadWorker,
    FetchWorker,
    PreviewLoader,
    SessionCheckWorker,
    open_in_explorer,
)


class MainWindow(QMainWindow):
    """应用主界面。"""

    def __init__(self, config: AppConfig):
        super().__init__()
        ensure_dirs()
        self.config = config
        # 状态库按「下载目录内的相对路径」判断已下载，换目录后依然有效
        self.state = StateStore(output_dir=config.output_dir)
        migrated = self.state.reindex()
        if migrated:
            self.state.save(force=True)
        self.cookies: list[CookieRecord] = load_cookies()
        self.campaign: Campaign | None = None
        self.posts: list[PostItem] = []

        self.fetch_worker: FetchWorker | None = None
        self.download_worker: DownloadWorker | None = None
        self.check_worker: SessionCheckWorker | None = None
        self._detected_post_id: str | None = None
        self._collections: list[Collection] = []
        self._suppress_collection_fetch = False
        self._preview_loaders: set[PreviewLoader] = set()
        self._preview_cache: dict[str, QPixmap] = {}
        self._preview_source: QPixmap | None = None
        self._status_cache: dict[str, tuple[str, str]] = {}

        self.setWindowTitle(f"{__app_name_cn__} v{__version__}")
        self.resize(1500, 950)
        self._build_ui()
        self._apply_theme()
        self._update_login_status()
        self._remember_to_input()
        self._on_creator_text_changed()

    # ================================================================== 构建
    def _build_ui(self) -> None:
        self._build_toolbar()

        self.post_model = PostTableModel(self)
        self.post_model.status_provider = self._status_for
        self.post_model.selection_changed.connect(self._on_selection_changed)

        self.media_model = MediaTableModel(self)
        self.queue_model = QueueTableModel(self)

        upper = QSplitter(Qt.Orientation.Horizontal)
        upper.addWidget(self._build_left_panel())
        upper.addWidget(self._build_detail_panel())
        upper.setStretchFactor(0, 3)
        upper.setStretchFactor(1, 2)

        lower = QTabWidget()
        lower.addTab(self._build_queue_panel(), "下载队列")
        lower.addTab(self._build_log_panel(), "运行日志")

        outer = QSplitter(Qt.Orientation.Vertical)
        outer.addWidget(upper)
        outer.addWidget(lower)
        outer.setStretchFactor(0, 4)
        outer.setStretchFactor(1, 1)
        outer.setSizes([680, 220])
        self.setCentralWidget(outer)

        # 状态栏
        status = QStatusBar()
        self.progress = QProgressBar()
        self.progress.setFixedWidth(260)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat("%p%")
        status.addPermanentWidget(self.progress)
        self.status_label = QLabel("就绪")
        status.addWidget(self.status_label, 1)
        self.setStatusBar(status)

    def _build_toolbar(self) -> None:
        bar = QToolBar("主工具栏")
        bar.setMovable(False)
        bar.setIconSize(QSize(18, 18))
        self.addToolBar(bar)

        bar.addWidget(QLabel(" 创作者 "))
        self.creator_edit = QLineEdit()
        self.creator_edit.setPlaceholderText(
            "创作者主页（如 BBebe、https://www.patreon.com/cw/BBebe），"
            "或某一篇作品的链接，或数字 ID"
        )
        self.creator_edit.setMinimumWidth(360)
        self.creator_edit.returnPressed.connect(self._on_fetch)
        self.creator_edit.textChanged.connect(self._on_creator_text_changed)
        bar.addWidget(self.creator_edit)

        self.creator_button = QToolButton()
        self.creator_button.setText("▾")
        self.creator_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.creator_menu = QMenu(self)
        self.creator_button.setMenu(self.creator_menu)
        bar.addWidget(self.creator_button)

        self.single_post_check = QCheckBox("只抓这一篇")
        self.single_post_check.setEnabled(False)
        self.single_post_check.setToolTip(
            "粘贴某一篇作品的链接后会自动勾上：只抓取这一篇，不拉取整个作品列表。\n"
            "想改成抓取该创作者的全部作品，取消勾选即可。"
        )
        self.single_post_check.toggled.connect(self._on_single_post_toggled)
        bar.addWidget(self.single_post_check)

        self.fetch_action = QAction("抓取作品列表", self)
        self.fetch_action.triggered.connect(self._on_fetch)
        bar.addAction(self.fetch_action)

        bar.addSeparator()

        self.login_action = QAction("登录 Patreon", self)
        self.login_action.triggered.connect(self._on_login)
        bar.addAction(self.login_action)

        self.check_action = QAction("检查登录状态", self)
        self.check_action.triggered.connect(self._on_check_session)
        bar.addAction(self.check_action)

        bar.addSeparator()

        self.settings_action = QAction("设置", self)
        self.settings_action.triggered.connect(self._on_settings)
        bar.addAction(self.settings_action)

        self.open_dir_action = QAction("打开下载目录", self)
        self.open_dir_action.triggered.connect(lambda: open_in_explorer(self.config.output_dir))
        bar.addAction(self.open_dir_action)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)

        self.login_status = QLabel("")
        self.login_status.setProperty("hint", True)
        bar.addWidget(self.login_status)

    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        box = QVBoxLayout(panel)
        box.setContentsMargins(8, 8, 4, 8)
        box.setSpacing(8)

        # 过滤行
        row = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("按标题 / 作品 ID 过滤…")
        self.filter_edit.textChanged.connect(self._apply_filter)
        row.addWidget(self.filter_edit, 1)

        self.kind_combo = QComboBox()
        self.kind_combo.addItem("全部类型", None)
        for key, label in (
            ("image_file", "图片集"),
            ("video_external_file", "视频"),
            ("video_embed", "外链视频"),
            ("audio_file", "音频"),
            ("text_only", "纯文字"),
            ("attachment", "附件"),
        ):
            self.kind_combo.addItem(label, key)
        self.kind_combo.currentIndexChanged.connect(self._apply_filter)
        row.addWidget(self.kind_combo)

        self.pending_check = QCheckBox("只看未下载")
        self.pending_check.toggled.connect(self._apply_filter)
        row.addWidget(self.pending_check)
        box.addLayout(row)

        # 合集行：同一个创作者的作品按他整理的合集筛选
        row_c = QHBoxLayout()
        row_c.addWidget(QLabel("合集"))
        self.collection_combo = QComboBox()
        self.collection_combo.setMinimumWidth(280)
        self.collection_combo.setToolTip(
            "按创作者整理的合集筛选作品。\n"
            "也可以直接把合集链接（patreon.com/collection/…）粘到上面的输入框。"
        )
        self.collection_combo.currentIndexChanged.connect(self._on_collection_changed)
        row_c.addWidget(self.collection_combo, 1)
        self.collection_hint = QLabel("")
        self.collection_hint.setProperty("hint", True)
        row_c.addWidget(self.collection_hint)
        box.addLayout(row_c)

        # 批量操作行
        row2 = QHBoxLayout()
        select_all = QPushButton("全选")
        select_all.clicked.connect(lambda: self.post_model.set_all_checked(True))
        select_none = QPushButton("全不选")
        select_none.clicked.connect(lambda: self.post_model.set_all_checked(False))
        select_pending = QPushButton("选中未下载")
        select_pending.clicked.connect(self._select_pending)
        row2.addWidget(select_all)
        row2.addWidget(select_none)
        row2.addWidget(select_pending)
        row2.addStretch(1)
        self.selection_label = QLabel("已选 0 篇")
        self.selection_label.setProperty("hint", True)
        row2.addWidget(self.selection_label)
        box.addLayout(row2)

        self.post_table = QTableView()
        self.post_table.setModel(self.post_model)
        self.post_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.post_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.post_table.setAlternatingRowColors(True)
        self.post_table.setSortingEnabled(True)
        self.post_table.sortByColumn(2, Qt.SortOrder.DescendingOrder)
        self.post_table.verticalHeader().setVisible(False)
        self.post_table.verticalHeader().setDefaultSectionSize(26)
        self.post_table.setWordWrap(False)
        self.post_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.post_table.customContextMenuRequested.connect(self._on_table_menu)
        self.post_table.doubleClicked.connect(self._on_table_double_click)
        self.post_table.selectionModel().selectionChanged.connect(
            lambda *_args: self._on_selection_changed_detail()
        )
        header = self.post_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.post_table.setColumnWidth(0, 34)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (2, 3, 4, 5, 6):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Fixed)
        self.post_table.setColumnWidth(7, 0)
        box.addWidget(self.post_table, 1)

        # 下载按钮行
        row3 = QHBoxLayout()
        self.download_checked_button = QPushButton("下载勾选的作品")
        self.download_checked_button.setProperty("accent", True)
        self.download_checked_button.clicked.connect(lambda: self._start_download(True))
        self.download_all_button = QPushButton("下载全部（增量）")
        self.download_all_button.clicked.connect(lambda: self._start_download(False))
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop_download)
        row3.addWidget(self.download_checked_button, 1)
        row3.addWidget(self.download_all_button, 1)
        row3.addWidget(self.stop_button)
        box.addLayout(row3)
        return panel

    def _build_detail_panel(self) -> QWidget:
        panel = QFrame()
        panel.setProperty("card", True)
        box = QVBoxLayout(panel)
        box.setContentsMargins(12, 12, 12, 12)
        box.setSpacing(8)

        self.detail_title = QLabel("未选择作品")
        self.detail_title.setProperty("heading", True)
        self.detail_title.setWordWrap(True)
        box.addWidget(self.detail_title)

        self.detail_meta = QLabel("")
        self.detail_meta.setProperty("hint", True)
        self.detail_meta.setWordWrap(True)
        self.detail_meta.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(self.detail_meta)

        self.preview_label = QLabel("（选中作品后显示预览图）")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumHeight(180)
        self.preview_label.setStyleSheet("border:1px dashed #3a4150; border-radius:6px;")
        box.addWidget(self.preview_label)

        row = QHBoxLayout()
        self.open_post_button = QPushButton("在浏览器打开作品页")
        self.open_post_button.setEnabled(False)
        self.open_post_button.clicked.connect(self._open_current_post)
        row.addWidget(self.open_post_button)
        self.open_folder_button = QPushButton("打开作品目录")
        self.open_folder_button.setEnabled(False)
        self.open_folder_button.clicked.connect(self._open_current_post_folder)
        row.addWidget(self.open_folder_button)
        box.addLayout(row)

        self.media_table = QTableView()
        self.media_table.setModel(self.media_model)
        self.media_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.media_table.setAlternatingRowColors(True)
        self.media_table.verticalHeader().setVisible(False)
        self.media_table.verticalHeader().setDefaultSectionSize(24)
        head = self.media_table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 6):
            head.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.media_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.media_table.customContextMenuRequested.connect(self._on_media_menu)
        box.addWidget(self.media_table, 1)

        self.detail_status = QLabel("")
        self.detail_status.setProperty("hint", True)
        self.detail_status.setWordWrap(True)
        box.addWidget(self.detail_status)
        return panel

    def _build_queue_panel(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(6, 6, 6, 6)
        self.queue_table = QTableView()
        self.queue_table.setModel(self.queue_model)
        self.queue_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.queue_table.setAlternatingRowColors(True)
        self.queue_table.verticalHeader().setVisible(False)
        head = self.queue_table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3, 4, 5):
            head.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        box.addWidget(self.queue_table)
        return page

    def _build_log_panel(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(6, 6, 6, 6)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        box.addWidget(self.log_view)

        row = QHBoxLayout()
        clear = QPushButton("清空日志")
        clear.clicked.connect(self.log_view.clear)
        row.addWidget(clear)
        row.addStretch(1)
        box.addLayout(row)
        return page

    # ================================================================== 主题
    def _apply_theme(self) -> None:
        self.setStyleSheet(stylesheet(self.config.theme))

    # ================================================================== 工具
    def log(self, message: str) -> None:
        self.log_view.append(message)
        self.status_label.setText(message[:120])

    def _remember_to_input(self) -> None:
        self.creator_menu.clear()
        if not self.config.recent_creators:
            action = QAction("（暂无历史记录）", self)
            action.setEnabled(False)
            self.creator_menu.addAction(action)
        else:
            for item in self.config.recent_creators:
                action = QAction(item, self)
                action.triggered.connect(lambda _checked=False, value=item: self._use_recent(value))
                self.creator_menu.addAction(action)
            self.creator_menu.addSeparator()
            clear = QAction("清空历史", self)
            clear.triggered.connect(self._clear_recent)
            self.creator_menu.addAction(clear)

    def _use_recent(self, value: str) -> None:
        self.creator_edit.setText(value)
        self._on_fetch()

    def _clear_recent(self) -> None:
        self.config.recent_creators = []
        self.config.save()
        self._remember_to_input()

    def _extract_options(self) -> ExtractOptions:
        return ExtractOptions(
            image_quality=self.config.image_quality,
            prefer_mux_full=self.config.prefer_mux_full,
            include_thumbnails=self.config.download_thumbnails,
            include_embeds=self.config.use_yt_dlp,
            include_previews=self.config.download_previews,
        )

    # ------------------------------------------------------ 单篇链接识别
    def _on_creator_text_changed(self) -> None:
        """输入框里换成另一篇作品的链接时，自动勾上「只抓这一篇」。"""
        text = self.creator_edit.text().strip()
        post_id: str | None = None
        if text:
            try:
                kind, values = parse_reference_all(text)
                if kind == "post" and values:
                    post_id = values[0]
            except PatreonError:
                post_id = None
        if post_id != self._detected_post_id:
            self._detected_post_id = post_id
            self.single_post_check.setChecked(bool(post_id))
        self.single_post_check.setEnabled(bool(post_id))

    def _on_single_post_toggled(self, checked: bool) -> None:
        if checked and self._detected_post_id:
            self.status_label.setText(f"只抓这一篇：作品 {self._detected_post_id}")
        elif self._detected_post_id:
            self.status_label.setText("已切换为抓取该创作者的全部作品")

    def _update_login_status(self) -> None:
        self.login_status.setText("  " + session_hint(self.cookies) + "  ")
        available, _error = browser_status()
        self.login_action.setToolTip(
            "在内置浏览器里登录 Patreon 并自动获取凭证" if available
            else "内置浏览器不可用，可在登录窗口里手动粘贴 Cookie"
        )

    # ============================================================== 登录相关
    def _on_login(self) -> None:
        dialog = LoginDialog(self)
        if dialog.exec() == LoginDialog.DialogCode.Accepted:
            self.cookies = dialog.cookies or load_cookies()
            self._update_login_status()
            self._status_cache.clear()
            self.log(f"已保存 {len(self.cookies)} 条登录凭证")
            self._on_check_session()
        else:
            self.cookies = load_cookies()
            self._update_login_status()

    def _on_check_session(self) -> None:
        if self.check_worker is not None and self.check_worker.isRunning():
            return
        if not self.cookies:
            QMessageBox.information(self, "尚未登录", "还没有登录凭证，请先点击「登录 Patreon」。")
            return
        self.check_worker = SessionCheckWorker(self.config, self.cookies, self)
        self.check_worker.result.connect(self._on_session_result)
        self.check_worker.failed.connect(lambda message: self.log(f"检查登录失败：{message}"))
        self.check_worker.start()

    @Slot(object)
    def _on_session_result(self, user: Any) -> None:
        if user:
            name = user.get("name") or user.get("vanity") or user.get("id")
            self.log(f"登录有效：{name}（{user.get('email') or '未提供邮箱'}）")
            self.status_label.setText(f"已登录：{name}")
        else:
            self.log("登录凭证无效或已过期，请重新登录 Patreon")
            QMessageBox.warning(
                self, "登录已失效",
                "Patreon 拒绝了当前登录凭证。请点击「登录 Patreon」重新登录。",
            )

    # ============================================================== 抓取相关
    def _on_fetch(self) -> None:
        reference = self.creator_edit.text().strip()
        single_post_id: str | None = None
        collection_id: str | None = None

        if self.single_post_check.isChecked():
            if not self._detected_post_id:
                QMessageBox.information(
                    self, "只抓这一篇",
                    "「只抓这一篇」需要在输入框里粘贴某一篇作品的链接。\n\n"
                    "想抓取某个创作者的全部作品，请取消勾选，"
                    "或直接把输入框换成创作者主页地址。",
                )
                return
            single_post_id = self._detected_post_id
            reference = reference or single_post_id
        elif reference:
            # 合集链接：直接抓这个合集（一次请求就够）
            try:
                kind, values = parse_reference_all(reference)
            except PatreonError:
                kind, values = "", []
            if kind == "collection" and values:
                collection_id = values[0]

        if not reference and not collection_id:
            QMessageBox.information(self, "请输入创作者", "请先填写创作者主页地址或名字。")
            return

        self._start_fetch(reference=reference, single_post_id=single_post_id,
                          collection_id=collection_id)

    def _start_fetch(self, reference: str = "", single_post_id: str | None = None,
                     collection_id: str | None = None,
                     fetch_collections: bool = True) -> None:
        if self.fetch_worker is not None and self.fetch_worker.isRunning():
            self.fetch_worker.stop()
            self.fetch_worker.wait(3000)

        self.post_model.clear()
        self.media_model.set_media([])
        self.posts = []
        self.campaign = None
        self._status_cache.clear()
        if collection_id is None:
            self._set_collections([])
        self.progress.setRange(0, 0)

        if single_post_id:
            self.log(f"开始抓取单篇作品：{single_post_id}")
            self.status_label.setText(f"正在抓取单篇作品 {single_post_id} …")
        elif collection_id:
            self.log(f"开始抓取合集：{collection_id}")
            self.status_label.setText(f"正在抓取合集 {collection_id} …")
        else:
            self.log(f"开始抓取：{reference}")

        self.fetch_worker = FetchWorker(
            self.config, self.cookies, reference, self._extract_options(),
            single_post_id=single_post_id, collection_id=collection_id,
            fetch_collections=fetch_collections, parent=self,
        )
        self.fetch_worker.campaign_ready.connect(self._on_campaign_ready)
        self.fetch_worker.collections_ready.connect(self._on_collections_ready)
        self.fetch_worker.collection_ready.connect(self._on_collection_ready)
        self.fetch_worker.posts_batch.connect(self._on_posts_batch)
        self.fetch_worker.page_progress.connect(self._on_fetch_progress)
        self.fetch_worker.inline_progress.connect(self._on_inline_progress)
        self.fetch_worker.failed.connect(self._on_fetch_failed)
        self.fetch_worker.completed.connect(self._on_fetch_completed)
        self.fetch_worker.start()

    @Slot(int)
    def _on_inline_progress(self, count: int) -> None:
        """正在逐个取回正文里内嵌的媒体。"""
        self.status_label.setText(f"正在获取正文内嵌媒体… 已取回 {count} 个")

    @Slot(object)
    def _on_campaign_ready(self, campaign: Campaign) -> None:
        self.campaign = campaign
        self.config.remember_creator(campaign.vanity or campaign.display_name)
        self.config.save()
        self._remember_to_input()
        single = self.single_post_check.isChecked() and self._detected_post_id
        self.setWindowTitle(
            f"{__app_name_cn__} v{__version__} — {campaign.display_name}"
            + (f" — 单篇 {self._detected_post_id}" if single else "")
        )
        self.log(f"创作者：{campaign.display_name}（campaign {campaign.id}，"
                 f"订阅者 {campaign.patron_count}）")

    @Slot(object)
    def _on_posts_batch(self, batch: list[PostItem]) -> None:
        for post in batch:
            post.campaign_id = post.campaign_id or (self.campaign.id if self.campaign else "")
        self.posts.extend(batch)
        self.post_model.append_posts(batch)
        self._apply_filter()

    @Slot(int, int)
    def _on_fetch_progress(self, fetched: int, total: int) -> None:
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(fetched)
        self.status_label.setText(f"已抓取 {fetched} / {total or '?'} 篇作品")

    @Slot(str)
    def _on_fetch_failed(self, message: str) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.log(f"抓取失败：{message}")
        QMessageBox.warning(self, "抓取失败", message)

    @Slot(int)
    def _on_fetch_completed(self, total: int) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(100 if total else 0)
        media_total = sum(len(p.media) for p in self.posts)
        inline_total = sum(p.inline_media_total for p in self.posts)
        inline_note = f"（含正文内嵌媒体 {inline_total} 个）" if inline_total else ""
        single_mode = self.single_post_check.isChecked() and bool(self._detected_post_id)

        if single_mode and self.posts:
            # 单篇模式：直接勾上，可以立刻点「下载勾选的作品」
            post = self.posts[0]
            self.post_model.set_checked(post.id, True)
            self._refresh_check_column()
            self.post_table.selectRow(0)
            self.log(f"单篇抓取完成：{post.safe_title[:60]}，"
                     f"共 {media_total} 个可下载条目{inline_note}（已自动勾选）")
            self.status_label.setText(
                f"单篇作品已抓取并勾选：{media_total} 个可下载条目"
            )
        else:
            self.log(f"抓取完成：{total} 篇作品，共 {media_total} 个可下载条目{inline_note}")
            self.status_label.setText(f"抓取完成：{total} 篇作品 / {media_total} 个文件")
        self._status_cache.clear()

    def _refresh_check_column(self) -> None:
        """让界面上的勾选框重画。"""
        rows = self.post_model.rowCount()
        if rows:
            top = self.post_model.index(0, 0)
            bottom = self.post_model.index(rows - 1, 0)
            self.post_model.dataChanged.emit(top, bottom, [Qt.ItemDataRole.CheckStateRole])

    # ============================================================== 过滤相关
    def _apply_filter(self) -> None:
        kinds: set[str] | None = None
        kind = self.kind_combo.currentData()
        if kind:
            kinds = {kind}
        self.post_model.apply_filter(
            keyword=self.filter_edit.text(),
            kinds=kinds,
            only_pending=self.pending_check.isChecked(),
            collection_ids=self._selected_collection_ids(),
        )
        self._update_collection_hint()

    # ---------------------------------------------------------- 合集
    def _current_collection(self) -> Collection | None:
        return self.collection_combo.currentData()

    def _selected_collection_ids(self) -> set[str] | None:
        """当前选中的合集包含哪些作品 id；选「全部」时返回 None。"""
        collection = self._current_collection()
        if collection is None:
            return None
        return set(collection.post_ids)

    def _set_collections(self, collections: list[Collection]) -> None:
        """填充合集下拉框，默认选中「全部作品」。"""
        self._collections = list(collections)
        blocked = self.collection_combo.blockSignals(True)
        try:
            self.collection_combo.clear()
            self.collection_combo.addItem(
                f"全部作品（{len(collections)} 个合集）" if collections else "全部作品", None
            )
            for collection in collections:
                self.collection_combo.addItem(collection.menu_label, collection)
        finally:
            self.collection_combo.blockSignals(blocked)
        self.collection_combo.setEnabled(bool(collections))
        self._update_collection_hint()

    def _select_collection(self, collection_id: str) -> bool:
        """在下拉框里选中指定合集；找不到返回 False。"""
        for index in range(self.collection_combo.count()):
            data = self.collection_combo.itemData(index)
            if isinstance(data, Collection) and data.id == str(collection_id):
                self.collection_combo.setCurrentIndex(index)
                return True
        return False

    def _update_collection_hint(self) -> None:
        if not getattr(self, "_collections", None):
            self.collection_hint.setText("")
            return
        collection = self._current_collection()
        if collection is None:
            self.collection_hint.setText(f"共 {len(self._collections)} 个合集")
            return
        loaded_ids = {p.id for p in self.posts}
        loaded = sum(1 for pid in collection.post_ids if pid in loaded_ids)
        total = collection.post_count or len(collection.post_ids)
        text = f"合集内 {loaded}/{total} 篇已加载"
        if loaded < total:
            text += "（切到「全部作品」再抓一次可补齐）"
        self.collection_hint.setText(text)

    def _on_collections_ready(self, collections: list[Collection]) -> None:
        self._set_collections(collections)
        self.log(f"该创作者有 {len(collections)} 个合集")

    def _on_collection_ready(self, collection: Collection) -> None:
        """抓取完某个合集后，自动在下拉框里选中它。"""
        self._suppress_collection_fetch = True
        try:
            if not self._select_collection(collection.id):
                self._set_collections([collection])
                self._select_collection(collection.id)
        finally:
            self._suppress_collection_fetch = False
        self._apply_filter()
        self.log(f"合集「{collection.display_name}」：{collection.post_count} 篇")

    def _on_collection_changed(self, _index: int) -> None:
        """切换合集：如果这个合集的作品一篇都没加载，就按合集抓回来。"""
        if self._suppress_collection_fetch:
            return
        collection = self._current_collection()
        if collection is not None and collection.post_ids:
            loaded_ids = {p.id for p in self.posts}
            if not any(pid in loaded_ids for pid in collection.post_ids):
                self.log(f"正在抓取合集「{collection.display_name}」…")
                self._start_fetch(collection_id=collection.id, fetch_collections=False)
                return
        self._apply_filter()

    def _select_pending(self) -> None:
        """把所有还没下完的作品勾选上。"""
        visible = self.post_model.visible_posts()
        for post in visible:
            text, _color = self._status_for(post)
            self.post_model.set_checked(post.id, post.can_view and "已下载" not in text)
        self._refresh_check_column()
        self.selection_label.setText(f"已选 {len(self.post_model.checked_posts())} 篇")

    def _on_selection_changed(self) -> None:
        count = len(self.post_model.checked_posts())
        self.selection_label.setText(f"已选 {count} 篇")

    # ========================================================== 状态与详情
    def _status_for(self, post: PostItem) -> tuple[str, str]:
        cached = self._status_cache.get(post.id)
        if cached:
            return cached
        media = filter_by_kind(post.media, self.config)
        if not post.can_view:
            result = ("需要订阅", STATUS_COLORS["locked"])
        elif not media:
            result = ("无可用内容", STATUS_COLORS["none"])
        else:
            done = sum(
                1 for item in media
                if self.state.is_downloaded(post.campaign_id, item.key, item.size, False)
            )
            if done == 0:
                result = (f"未下载 0/{len(media)}", STATUS_COLORS["none"])
            elif done >= len(media):
                result = (f"已下载 {done}/{len(media)}", STATUS_COLORS["ok"])
            else:
                result = (f"部分 {done}/{len(media)}", STATUS_COLORS["partial"])
        self._status_cache[post.id] = result
        return result

    def _current_post(self) -> PostItem | None:
        indexes = self.post_table.selectionModel().selectedRows()
        if not indexes:
            return None
        return self.post_model.post_at(indexes[0].row())

    def _on_selection_changed_detail(self) -> None:
        post = self._current_post()
        if post is None:
            self.detail_title.setText("未选择作品")
            self.detail_meta.setText("")
            self.media_model.set_media([])
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("（选中作品后显示预览图）")
            self.open_post_button.setEnabled(False)
            self.open_folder_button.setEnabled(False)
            self.detail_status.setText("")
            return

        self.detail_title.setText(post.safe_title)
        self.detail_meta.setText(
            f"发布时间：{post.date_text}\n"
            f"类型：{post.post_type}    媒体：{post.count_text()}\n"
            f"链接：{post.url}"
        )
        media = filter_by_kind(post.media, self.config)
        self.media_model.set_media(media)
        self.open_post_button.setEnabled(bool(post.url))
        self.open_folder_button.setEnabled(self.campaign is not None)

        text, color = self._status_for(post)
        extra = ""
        if not post.can_view:
            extra = "（该作品需要更高等级的订阅才能访问）"
        total = post.total_size()
        self.detail_status.setText(
            f'状态：<span style="color:{color}">{text}</span>{extra}'
            + (f"   合计大小约 {human_size(total)}" if total else "")
        )

        first_image = next((m for m in media if m.kind == "image"), None)
        if first_image:
            self._load_preview(first_image.url, post.url)
        else:
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("（该作品没有可预览的图片）")

    def _load_preview(self, url: str, referer: str) -> None:
        cached = self._preview_cache.get(url)
        if cached is not None:
            self._show_preview(cached)
            return
        self.preview_label.setPixmap(QPixmap())
        self.preview_label.setText("正在加载预览图…")
        loader = PreviewLoader(url, referer, self.config.proxy, self)
        self._preview_loaders.add(loader)
        loader.loaded.connect(self._on_preview_loaded)
        loader.finished.connect(lambda: self._preview_loaders.discard(loader))
        loader.start()

    @Slot(object, object)
    def _on_preview_loaded(self, url: str, data: Any) -> None:
        if not data:
            self.preview_label.setText("（预览图加载失败）")
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            self.preview_label.setText("（预览图格式不支持）")
            return
        self._preview_cache[url] = pixmap
        self._show_preview(pixmap)

    def _show_preview(self, pixmap: QPixmap) -> None:
        self._preview_source = pixmap
        available = self.preview_label.size()
        if available.width() < 20 or available.height() < 20:
            return
        scaled = pixmap.scaled(
            available * 0.98,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.preview_label.setText("")
        self.preview_label.setPixmap(scaled)

    def _on_table_double_click(self, index) -> None:
        post = self.post_model.post_at(index.row())
        if post and post.url:
            QDesktopServices.openUrl(QUrl(post.url))

    def _open_current_post(self) -> None:
        post = self._current_post()
        if post and post.url:
            QDesktopServices.openUrl(QUrl(post.url))

    def _open_current_post_folder(self) -> None:
        post = self._current_post()
        if post is None or self.campaign is None:
            return
        from ..naming import post_directory

        directory = post_directory(self.config, self.campaign, post)
        if os.path.isdir(directory):
            open_in_explorer(directory)
        else:
            open_in_explorer(self.config.output_dir)

    def _on_table_menu(self, position) -> None:
        post = self.post_model.post_at(self.post_table.indexAt(position).row())
        menu = QMenu(self)
        if post is not None:
            open_action = menu.addAction("在浏览器打开作品页")
            open_action.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(post.url)))
            copy_action = menu.addAction("复制作品链接")
            copy_action.triggered.connect(lambda: self._copy_text(post.url))
            menu.addSeparator()
            only_action = menu.addAction("只下载这一篇")
            only_action.triggered.connect(lambda: self._start_download(True, only={post.id}))
            menu.addSeparator()
        check_action = menu.addAction("全选当前列表")
        check_action.triggered.connect(lambda: self.post_model.set_all_checked(True))
        uncheck_action = menu.addAction("取消全选")
        uncheck_action.triggered.connect(lambda: self.post_model.set_all_checked(False))
        menu.exec(self.post_table.viewport().mapToGlobal(position))

    def _on_media_menu(self, position) -> None:
        item = self.media_model.item_at(self.media_table.indexAt(position).row())
        if item is None:
            return
        menu = QMenu(self)
        copy_action = menu.addAction("复制下载链接")
        copy_action.triggered.connect(lambda: self._copy_text(item.url))
        open_action = menu.addAction("在浏览器打开")
        open_action.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(item.url)))
        menu.exec(self.media_table.viewport().mapToGlobal(position))

    def _copy_text(self, text: str) -> None:
        QGuiApplication.clipboard().setText(text or "")
        self.status_label.setText("已复制到剪贴板")

    # ============================================================== 下载相关
    def _start_download(self, only_checked: bool, only: set[str] | None = None) -> None:
        if self.download_worker is not None and self.download_worker.isRunning():
            QMessageBox.information(self, "正在下载", "当前还有下载任务在运行，请先停止或等待完成。")
            return
        if self.campaign is None:
            QMessageBox.information(self, "请先抓取", "请先抓取创作者的作品列表。")
            return

        if only is not None:
            selected_ids = only
            posts = [p for p in self.posts if p.id in only]
        elif only_checked:
            selected_ids = self.post_model.checked_ids()
            posts = self.post_model.checked_posts()
            if not selected_ids:
                QMessageBox.information(self, "未勾选", "请先勾选要下载的作品（第一列的复选框）。")
                return
        else:
            selected_ids = None
            posts = [
                p for p in self.posts
                if filter_by_kind(p.media, self.config)
            ]

        if not posts:
            QMessageBox.information(self, "没有内容", "当前条件下没有可下载的作品。")
            return

        total = sum(len(filter_by_kind(p.media, self.config)) for p in posts)
        self.queue_model.clear()
        self.progress.setRange(0, total)
        self.progress.setValue(0)
        self.stop_button.setEnabled(True)
        self.download_checked_button.setEnabled(False)
        self.download_all_button.setEnabled(False)
        self.log(f"开始下载：{len(posts)} 篇作品 / {total} 个文件")

        self.download_worker = DownloadWorker(
            self.config, self.cookies, self.state, self.campaign, posts,
            selected_ids, self,
        )
        self.download_worker.task_started.connect(self._on_task_started)
        self.download_worker.task_progress.connect(self._on_task_progress)
        self.download_worker.task_done.connect(self._on_task_done)
        self.download_worker.queue_progress.connect(self._on_queue_progress)
        self.download_worker.log.connect(self.log)
        self.download_worker.completed.connect(self._on_download_completed)
        self.download_worker.start()

    def _stop_download(self) -> None:
        if self.download_worker is not None:
            self.log("正在停止下载…")
            self.download_worker.stop()
            self.stop_button.setEnabled(False)

    @Slot(object)
    def _on_task_started(self, task) -> None:
        self.queue_model.add_task(task)

    @Slot(object, int, int)
    def _on_task_progress(self, task, done: int, total: int) -> None:
        self.queue_model.update_progress(task, done, total)

    @Slot(object)
    def _on_task_done(self, result: TaskResult) -> None:
        labels = {"done": "已完成", "skipped": "已跳过", "failed": "失败", "stopped": "已停止"}
        self.queue_model.set_status(result.task, labels.get(result.status, result.status))
        if result.status == "failed":
            self.log(f"❌ {result.task.filename} — {result.error}")
        self._status_cache.pop(result.task.post.id, None)

    @Slot(int, int)
    def _on_queue_progress(self, done: int, total: int) -> None:
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(done)
        self.status_label.setText(f"下载进度 {done}/{total}")

    @Slot(object)
    def _on_download_completed(self, summary: dict[str, Any]) -> None:
        self.stop_button.setEnabled(False)
        self.download_checked_button.setEnabled(True)
        self.download_all_button.setEnabled(True)
        self._status_cache.clear()
        if self.post_model.rowCount():
            top = self.post_model.index(0, 6)
            bottom = self.post_model.index(self.post_model.rowCount() - 1, 6)
            self.post_model.dataChanged.emit(top, bottom)
        self._on_selection_changed_detail()

        done = summary.get("done", 0)
        skipped = summary.get("skipped", 0)
        failed = summary.get("failed", 0)
        size_text = summary.get("size_text", "-")
        self.log(f"下载结束：新下载 {done}，跳过 {skipped}，失败 {failed}，共 {size_text}")
        self.status_label.setText(
            f"下载结束：新下载 {done} / 跳过 {skipped} / 失败 {failed} / {size_text}"
        )
        if failed:
            errors = summary.get("errors") or []
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("部分文件下载失败")
            box.setText(f"有 {failed} 个文件下载失败。")
            box.setDetailedText("\n".join(errors[:50]) or "（无详情）")
            box.exec()
        if self.config.open_folder_when_done:
            open_in_explorer(self.config.output_dir)

    # ============================================================== 设置相关
    def _on_settings(self) -> None:
        dialog = SettingsDialog(self.config, self)
        if dialog.exec() == SettingsDialog.DialogCode.Accepted:
            previous_output = self.config.output_dir
            self.config = dialog.config
            self.config.save()
            # 下载目录可能变了：让状态库按新目录重新解析相对路径
            self.state.set_output_dir(self.config.output_dir)
            if os.path.normcase(os.path.abspath(previous_output)) != \
                    os.path.normcase(os.path.abspath(self.config.output_dir)):
                filled = self.state.reindex()
                self.log(f"下载目录已切换到 {self.config.output_dir}"
                         + (f"，已重整 {filled} 条记录" if filled else ""))
            self._apply_theme()
            self._status_cache.clear()
            self.log("设置已保存")
            if self.post_model.rowCount():
                top = self.post_model.index(0, 0)
                bottom = self.post_model.index(self.post_model.rowCount() - 1, 6)
                self.post_model.dataChanged.emit(top, bottom)
            self._on_selection_changed_detail()

    # ============================================================== 生命周期
    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        source = getattr(self, "_preview_source", None)
        if source is not None and not source.isNull():
            self._show_preview(source)

    def closeEvent(self, event) -> None:  # noqa: N802
        for worker in (self.fetch_worker, self.download_worker, self.check_worker):
            if worker is not None and worker.isRunning():
                if hasattr(worker, "stop"):
                    worker.stop()
                worker.wait(5000)
        for loader in list(self._preview_loaders):
            loader.wait(1000)
        self.state.save(force=True)
        try:
            self.config.save()
        except OSError:
            pass
        super().closeEvent(event)
