"""内置浏览器登录窗口：登录 Patreon 后自动捕获 session_id 等凭证。"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import USER_AGENT, WEB_PROFILE_DIR, ensure_dirs
from ..cookies import (
    CookieRecord,
    has_session,
    merge_cookies,
    parse_cookie_header,
    save_cookies,
    session_id_of,
)

try:  # pragma: no cover - 取决于运行环境
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView

    WEBENGINE_AVAILABLE = True
    WEBENGINE_ERROR = ""
except Exception as exc:  # noqa: BLE001
    WEBENGINE_AVAILABLE = False
    WEBENGINE_ERROR = str(exc)
    QWebEngineProfile = None  # type: ignore[assignment]
    QWebEnginePage = None  # type: ignore[assignment]
    QWebEngineView = None  # type: ignore[assignment]


PATREON_LOGIN_URL = "https://www.patreon.com/login"
PATREON_HOME_URL = "https://www.patreon.com/"

# 整个进程共用一个持久化 profile：页面可以随窗口销毁，profile 活到程序退出，
# 这样登录状态跨次启动保留，也不会出现 "profile released but page still alive" 的警告。
_SHARED_PROFILE = None


def shared_profile():
    """懒加载的 QWebEngineProfile（带磁盘缓存与持久化 Cookie）。"""
    global _SHARED_PROFILE
    if not WEBENGINE_AVAILABLE:
        return None
    if _SHARED_PROFILE is None:
        from PySide6.QtWidgets import QApplication

        owner = QApplication.instance() or None
        profile = QWebEngineProfile("patreon-profile", owner)
        profile.setPersistentStoragePath(str(WEB_PROFILE_DIR))
        profile.setCachePath(str(WEB_PROFILE_DIR / "cache"))
        profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
        profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
        )
        profile.setHttpUserAgent(USER_AGENT)
        _SHARED_PROFILE = profile
    return _SHARED_PROFILE


def _to_text(value: Any) -> str:
    """QByteArray / bytes / str 统一成 str。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    data = getattr(value, "data", None)
    if callable(data):
        try:
            return bytes(data()).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
    return str(value)


class CookieCollector(QObject):
    """挂在内置浏览器 profile 上，收集 Patreon 域名的 cookie。"""

    changed = Signal(int, bool)   # cookie 总数, 是否已有 session_id

    def __init__(self, profile, parent: QObject | None = None):
        super().__init__(parent)
        self.profile = profile
        self._records: dict[tuple[str, str, str], CookieRecord] = {}

    def attach(self) -> None:
        if self.profile is None:
            return
        store = self.profile.cookieStore()
        store.cookieAdded.connect(self._on_cookie_added)
        try:
            store.cookieRemoved.connect(self._on_cookie_removed)
        except Exception:  # noqa: BLE001
            pass
        store.loadAllCookies()

    def refresh(self) -> None:
        if self.profile is not None:
            self.profile.cookieStore().loadAllCookies()

    def _on_cookie_added(self, cookie) -> None:
        try:
            name = _to_text(cookie.name())
            value = _to_text(cookie.value())
            if not name:
                return
            domain = _to_text(cookie.domain()) or ".patreon.com"
            path = _to_text(cookie.path()) or "/"
            expires = None
            try:
                expiration = cookie.expirationDate()
                if expiration is not None and expiration.isValid():
                    expires = int(expiration.toSecsSinceEpoch())
            except Exception:  # noqa: BLE001
                expires = None
            record = CookieRecord(
                name=name,
                value=value,
                domain=domain,
                path=path,
                secure=bool(cookie.isSecure()),
                expires=expires,
                http_only=bool(cookie.isHttpOnly()),
            )
            self._records[(record.name, record.domain, record.path)] = record
            self._emit()
        except Exception:  # noqa: BLE001
            return

    def _on_cookie_removed(self, cookie) -> None:
        try:
            name = _to_text(cookie.name())
            domain = _to_text(cookie.domain()) or ".patreon.com"
            path = _to_text(cookie.path()) or "/"
            self._records.pop((name, domain, path), None)
            self._emit()
        except Exception:  # noqa: BLE001
            return

    def _emit(self) -> None:
        records = self.records()
        self.changed.emit(len(records), has_session(records))

    def records(self) -> list[CookieRecord]:
        from ..cookies import is_patreon_domain

        return [r for r in self._records.values() if is_patreon_domain(r.domain)]

    def clear(self) -> None:
        self._records.clear()
        self._emit()


class LoginDialog(QDialog):
    """登录对话框：内置浏览器 + 手动粘贴两种方式。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("登录 Patreon")
        self.resize(1080, 760)
        self.setSizeGripEnabled(True)

        self.cookies: list[CookieRecord] = []
        self.collector: CookieCollector | None = None
        self.profile = None
        self.view = None

        ensure_dirs()
        self._build_ui()

    # ------------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("登录 Patreon 以获取访问付费内容的凭证")
        title.setProperty("heading", True)
        layout.addWidget(title)

        hint = QLabel(
            "登录成功后，程序会自动抓取 <b>session_id</b> 等登录凭证并加密保存在本机 "
            "<code>data/cookies.json</code>，之后所有下载都使用这份凭证。<br>"
            "凭证只保存在你自己的电脑上，不会上传到任何地方。"
        )
        hint.setProperty("hint", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_browser_tab(), "内置浏览器登录（推荐）")
        self.tabs.addTab(self._build_manual_tab(), "手动粘贴 Cookie")
        layout.addWidget(self.tabs, 1)

        # 底部状态与按钮
        bottom = QHBoxLayout()
        self.status_label = QLabel("尚未检测到登录凭证")
        self.status_label.setProperty("hint", True)
        bottom.addWidget(self.status_label, 1)

        self.refresh_button = QPushButton("重新检测登录状态")
        self.refresh_button.clicked.connect(self._refresh_cookies)
        bottom.addWidget(self.refresh_button)

        buttons = QDialogButtonBox()
        self.ok_button = buttons.addButton("完成并保存凭证", QDialogButtonBox.ButtonRole.AcceptRole)
        self.ok_button.setProperty("accent", True)
        cancel = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        self.ok_button.clicked.connect(self._accept)
        cancel.clicked.connect(self.reject)
        bottom.addWidget(buttons)
        layout.addLayout(bottom)

    def _build_browser_tab(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 8, 0, 0)
        box.setSpacing(8)

        nav = QHBoxLayout()
        self.back_button = QPushButton("← 后退")
        self.forward_button = QPushButton("前进 →")
        self.reload_button = QPushButton("刷新")
        self.login_button = QPushButton("打开 Patreon 登录页")
        self.login_button.setProperty("accent", True)
        for button in (self.back_button, self.forward_button, self.reload_button, self.login_button):
            nav.addWidget(button)
        nav.addStretch(1)
        self.url_label = QLabel("")
        self.url_label.setProperty("hint", True)
        self.url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        nav.addWidget(self.url_label)
        box.addLayout(nav)

        if not WEBENGINE_AVAILABLE:
            warn = QLabel(
                "内置浏览器不可用（未能加载 QtWebEngine）：<br>"
                f"<code>{WEBENGINE_ERROR}</code><br><br>"
                "请改用「手动粘贴 Cookie」标签页，或在命令行执行：<br>"
                "<code>pip install PySide6</code> 后重新启动。"
            )
            warn.setWordWrap(True)
            warn.setStyleSheet("color:#ff6b6b;")
            box.addWidget(warn)
            box.addStretch(1)
            for button in (self.back_button, self.forward_button, self.reload_button,
                           self.login_button):
                button.setEnabled(False)
            return page

        # 使用进程级持久化 profile，登录状态可以跨次启动保留
        self.profile = shared_profile()
        if self.profile is None:
            box.addWidget(QLabel("浏览器配置初始化失败，请改用手动粘贴 Cookie。"))
            box.addStretch(1)
            return page

        self.collector = CookieCollector(self.profile, self)
        self.collector.changed.connect(self._on_cookies_changed)

        self.view = QWebEngineView(page)
        web_page = QWebEnginePage(self.profile, self.view)
        self.view.setPage(web_page)
        self.view.urlChanged.connect(lambda url: self.url_label.setText(url.toString()))
        self.view.loadFinished.connect(self._on_load_finished)

        self.back_button.clicked.connect(self.view.back)
        self.forward_button.clicked.connect(self.view.forward)
        self.reload_button.clicked.connect(self.view.reload)
        self.login_button.clicked.connect(self._open_login_page)

        # 页面内的 target=_blank 一律在本窗口打开
        try:
            self.view.page().newWindowRequested.connect(self._on_new_window)
        except Exception:  # noqa: BLE001
            pass

        box.addWidget(self.view, 1)
        self.collector.attach()
        self.view.load(QUrl(PATREON_LOGIN_URL))
        return page

    def _build_manual_tab(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 8, 0, 0)
        box.setSpacing(8)

        tip = QLabel(
            "在已经登录 Patreon 的浏览器里按 <b>F12</b> 打开开发者工具 → "
            "<b>Application/应用</b> → <b>Cookies</b> → <code>https://www.patreon.com</code>，"
            "复制 <code>session_id</code> 的值，按下面的任一格式粘贴：<br>"
            "· <code>session_id=xxxxxxxx</code><br>"
            "· <code>session_id=xxxx; patreon_device_id=yyyy</code>（可粘贴多行整段 Cookie 头）"
        )
        tip.setWordWrap(True)
        box.addWidget(tip)

        self.manual_edit = QPlainTextEdit()
        self.manual_edit.setPlaceholderText("session_id=……")
        box.addWidget(self.manual_edit, 1)

        row = QHBoxLayout()
        self.paste_button = QPushButton("从剪贴板粘贴")
        self.paste_button.clicked.connect(self._paste_clipboard)
        row.addWidget(self.paste_button)
        parse_button = QPushButton("解析并检查")
        parse_button.clicked.connect(self._parse_manual)
        row.addWidget(parse_button)
        row.addStretch(1)
        self.manual_status = QLabel("")
        self.manual_status.setProperty("hint", True)
        row.addWidget(self.manual_status)
        box.addLayout(row)
        return page

    # ---------------------------------------------------------------- 行为
    def _open_login_page(self) -> None:
        if self.view is not None:
            self.view.load(QUrl(PATREON_LOGIN_URL))

    def _on_new_window(self, request) -> None:
        try:
            self.view.setUrl(request.requestedUrl())
        except Exception:  # noqa: BLE001
            pass

    def _on_load_finished(self, ok: bool) -> None:
        if self.collector is not None:
            self.collector.refresh()

    def _on_cookies_changed(self, count: int, has_session_id: bool) -> None:
        if has_session_id:
            self.status_label.setText(
                f"✅ 已捕获登录凭证（共 {count} 条 Patreon Cookie，含 session_id），可以点击“完成并保存凭证”"
            )
        elif count:
            self.status_label.setText(f"已捕获 {count} 条 Cookie，但还没有 session_id —— 请先完成登录")
        else:
            self.status_label.setText("尚未检测到登录凭证")

    def _refresh_cookies(self) -> None:
        if self.collector is not None:
            self.collector.refresh()
        self._parse_manual(silent=True)

    def _paste_clipboard(self) -> None:
        text = QGuiApplication.clipboard().text()
        if text:
            self.manual_edit.setPlainText(text.strip())
            self._parse_manual()

    def _parse_manual(self, silent: bool = False) -> None:
        records = parse_cookie_header(self.manual_edit.toPlainText())
        if not records:
            if not silent:
                self.manual_status.setText("没有解析到任何 Cookie")
            return
        if has_session(records):
            self.manual_status.setText(f"✅ 解析到 {len(records)} 条 Cookie，包含 session_id")
        else:
            self.manual_status.setText(
                f"解析到 {len(records)} 条 Cookie，但没有 session_id（可能无法访问付费内容）"
            )

    def collected_cookies(self) -> list[CookieRecord]:
        """合并两种方式得到的 cookie。"""
        records: list[CookieRecord] = []
        if self.collector is not None:
            records = self.collector.records()
        manual = parse_cookie_header(self.manual_edit.toPlainText())
        return merge_cookies(records, manual)

    def _accept(self) -> None:
        records = self.collected_cookies()
        if not records:
            self.status_label.setText("❌ 没有任何 Cookie，请先在内置浏览器里登录 Patreon")
            self.tabs.setCurrentIndex(0 if WEBENGINE_AVAILABLE else 1)
            return
        self.cookies = records
        save_cookies(records)
        self.accept()

    # ------------------------------------------------------------- 生命周期
    def closeEvent(self, event) -> None:  # noqa: N802
        self._teardown()
        super().closeEvent(event)

    def done(self, result: int) -> None:  # noqa: D102
        self._teardown()
        super().done(result)

    def _teardown(self) -> None:
        """先释放页面，再放走 profile，避免 QtWebEngine 的释放顺序警告。"""
        if self.collector is not None:
            try:
                self.collector.changed.disconnect()
            except (RuntimeError, TypeError):
                pass
        if self.view is not None:
            try:
                self.view.stop()
                self.view.setUrl(QUrl("about:blank"))
                self.view.setParent(None)
                self.view.deleteLater()
            except RuntimeError:
                pass
            self.view = None
        self.profile = None


def browser_status() -> tuple[bool, str]:
    """给主窗口用：内置浏览器是否可用。"""
    return WEBENGINE_AVAILABLE, WEBENGINE_ERROR


def session_hint(records: list[CookieRecord]) -> str:
    """返回一句登录状态描述。"""
    if not records:
        return "未登录"
    if has_session(records):
        sid = session_id_of(records)
        return f"已登录（session_id …{sid[-6:]}）" if sid else "已登录"
    return "凭证不完整（缺少 session_id）"
