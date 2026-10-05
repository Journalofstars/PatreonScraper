"""应用入口：创建 QApplication 并显示主窗口。"""

from __future__ import annotations

import os
import sys
import traceback

from .. import __app_name_cn__, __version__
from ..config import LOG_DIR, AppConfig, ensure_dirs

def _prepare_environment(software_rendering: bool = False) -> None:
    """设置 QtWebEngine 需要的环境变量（必须在 QApplication 之前）。"""
    flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    if software_rendering and "--disable-gpu" not in flags:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (flags + " --disable-gpu --disable-software-rasterizer").strip()
    # 让 Chromium 把日志级别降下来，避免刷屏
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", flags or "--disable-logging")


def _install_excepthook() -> None:
    """把未捕获异常写进日志文件，方便排查。"""

    def hook(exc_type, exc_value, exc_tb):
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            ensure_dirs()
            with open(os.path.join(str(LOG_DIR), "crash.log"), "a", encoding="utf-8") as handle:
                handle.write(text + "\n")
        except OSError:
            pass
        # 无控制台的打包版里 sys.stderr 是 None，判空后再写
        stream = sys.stderr
        if stream is not None:
            try:
                stream.write(text)
            except Exception:  # noqa: BLE001
                pass

    sys.excepthook = hook


def _run_selftest() -> int:
    """自检：确认这份程序（尤其是打包后的 exe）各项依赖都完整。

    检查数据目录可写、Qt 平台插件、QtWebEngine（内置浏览器）、网络库，
    结果同时打印到界面/控制台并写入 ``data/logs/selftest.log``。
    """
    from ..config import APP_DIR, DATA_DIR, ensure_dirs

    lines: list[str] = []
    failures = 0

    def record(ok: bool, label: str, detail: str = "") -> None:
        nonlocal failures
        if not ok:
            failures += 1
        mark = "通过" if ok else "失败"
        lines.append(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))

    lines.append(f"Patreon 内容下载器 自检（v{__version__}）")
    lines.append(f"冻结打包: {getattr(sys, 'frozen', False)}")
    lines.append(f"数据目录: {APP_DIR}")
    lines.append("-" * 58)

    def check(label: str, func) -> None:
        try:
            detail = func()
            record(True, label, str(detail) if detail else "")
        except Exception as exc:  # noqa: BLE001
            record(False, label, f"{type(exc).__name__}: {exc}")

    # 1) 数据目录可写
    def _data_dir():
        ensure_dirs()
        probe = DATA_DIR / ".selftest"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return str(DATA_DIR)

    check("数据目录可读写", _data_dir)

    # 2) 网络库
    def _requests():
        import requests

        return f"requests {requests.__version__}"

    check("HTTP 客户端", _requests)

    # 3) Qt 平台插件（能建出 QApplication 就说明插件齐了）
    from PySide6.QtWidgets import QApplication

    check("Qt 平台插件", lambda: QApplication.platformName() or "ok")

    # 4) 能否连上 Patreon
    def _network():
        import requests

        from ..config import USER_AGENT

        response = requests.get(
            "https://www.patreon.com/", timeout=20,
            headers={"User-Agent": USER_AGENT},
        )
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}")
        return f"HTTP {response.status_code}，{len(response.content)} 字节"

    check("访问 patreon.com", _network)

    # 5) QtWebEngine：内置浏览器能否真正加载页面
    def _webengine():
        from PySide6.QtCore import QEventLoop, QTimer, QUrl
        from PySide6.QtWebEngineCore import QWebEnginePage
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWidgets import QApplication as _App

        from .login_window import shared_profile

        app = _App.instance()
        # 用与登录窗口相同的进程级 profile：它活到进程退出，
        # 不会出现「profile 先于 page 销毁」导致的退出崩溃
        profile = shared_profile()
        view = QWebEngineView()
        if profile is not None:
            view.setPage(QWebEnginePage(profile, view))
        loop = QEventLoop()
        state = {"ok": False, "error": ""}

        def finished(success: bool) -> None:
            state["ok"] = bool(success)
            if not success:
                state["error"] = "页面加载失败"
            loop.quit()

        view.loadFinished.connect(finished)
        view.load(QUrl("about:blank"))
        QTimer.singleShot(15000, loop.quit)
        loop.exec()
        view.stop()
        view.setParent(None)
        view.deleteLater()
        for _ in range(5):
            app.processEvents()
        if not state["ok"]:
            raise RuntimeError(state["error"] or "15 秒内没有加载完成")
        return "QWebEngineView 可加载页面"

    check("内置浏览器 (QtWebEngine)", _webengine)

    lines.append("-" * 58)
    if failures:
        lines.append(f"结论：有 {failures} 项未通过，这份程序可能不完整。")
    else:
        lines.append("结论：全部通过，程序可以正常使用。")

    report = "\n".join(lines)
    try:
        ensure_dirs()
        (LOG_DIR / "selftest.log").write_text(report + "\n", encoding="utf-8")
    except OSError:
        pass

    # 注意：无控制台的打包版里 sys.stdout / sys.stderr 是 None，
    # print() 会静默跳过，但 .flush() 会抛 AttributeError，必须判空。
    text_stream = sys.stdout
    if text_stream is not None:
        try:
            text_stream.write(report + "\n")
            text_stream.flush()
        except Exception:  # noqa: BLE001
            text_stream = None

    if text_stream is None:
        # 双击运行的打包版看不到控制台，用弹窗把结果显示出来
        try:
            from PySide6.QtWidgets import QMessageBox

            box = QMessageBox()
            box.setWindowTitle("自检结果")
            box.setIcon(QMessageBox.Icon.Information if not failures
                        else QMessageBox.Icon.Warning)
            box.setText("自检全部通过，程序可以正常使用。" if not failures
                        else f"有 {failures} 项未通过，请查看下面的详情。")
            box.setDetailedText(report)
            box.exec()
        except Exception:  # noqa: BLE001
            pass

    # QtWebEngine 在解释器退出阶段销毁顺序很脆弱，正常 return 会以
    # 0xC0000005（段错误）结束，让人误以为自检失败。报告已经写出去了，
    # 这里直接结束进程，保证退出码如实反映自检结果。
    os._exit(0 if failures == 0 else 1)


def run(argv: list[str] | None = None, software_rendering: bool | None = None) -> int:
    """启动图形界面，返回进程退出码。"""
    argv = list(sys.argv if argv is None else argv)
    if software_rendering is None:
        software_rendering = (
            "--software" in argv or os.environ.get("PATREON_DL_SOFTWARE_GL") == "1"
        )
    selftest = "--selftest" in argv
    argv = [a for a in argv if a not in ("--software", "--selftest")]

    _prepare_environment(software_rendering)
    _install_excepthook()
    ensure_dirs()

    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication, QMessageBox

    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)
    QCoreApplication.setApplicationName("PatreonDownloader")
    QCoreApplication.setApplicationVersion(__version__)
    QCoreApplication.setOrganizationName("PatreonDownloader")

    app = QApplication(argv)
    app.setApplicationDisplayName(__app_name_cn__)
    font = QFont("Microsoft YaHei UI", 9)
    app.setFont(font)

    if selftest:
        return _run_selftest()

    config = AppConfig.load()

    from .main_window import MainWindow

    try:
        window = MainWindow(config)
    except Exception as exc:  # noqa: BLE001
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("启动失败")
        box.setText(f"主窗口初始化失败：{exc}")
        box.setDetailedText(traceback.format_exc())
        box.exec()
        return 2

    window.show()
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
