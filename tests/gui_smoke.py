"""界面冒烟测试：离屏构建主窗口，灌入假数据并截图。

用法：
    .venv\\Scripts\\python.exe tests\\gui_smoke.py [输出目录]

默认截图写到 <项目根>\\data\\shots\\。
不需要网络，也不会弹出窗口（使用 Qt 的 offscreen 平台）。
"""

from __future__ import annotations

import os
import sys
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def build_fake_posts(count: int = 6):
    from patreon_dl.models import MediaItem, PostItem

    posts = []
    for i in range(count):
        media = []
        for n in range(1, 4):
            is_video = n % 3 == 0
            media.append(
                MediaItem(
                    url=f"https://c10.patreonusercontent.com/4/patreon-media/p/post/1/x/{n}/1.jpg",
                    kind="video" if is_video else "image",
                    filename=f"clip {n}.mp4" if is_video else f"file {n}.jpg",
                    mimetype="video/mp4" if is_video else "image/jpeg",
                    size=123456 * (n + 1),
                    post_id=str(1000 + i),
                    media_id=str(n),
                )
            )
        posts.append(
            PostItem(
                id=str(1000 + i),
                title=f"测试作品 {i} — 标题 with emoji ✦🖐🏻",
                published_at=f"2025-0{(i % 9) + 1}-1{i}T10:00:00.000+00:00",
                post_type="image_file" if i % 2 else "video_external_file",
                url=f"https://www.patreon.com/posts/test-{1000 + i}",
                campaign_id="122089",
                can_view=(i != 3),
                media=media,
            )
        )
    return posts


def build_fake_collections():
    from patreon_dl.models import Collection

    return [
        Collection(id="2084280", title="| Multiple Character Videos |", post_count=12,
                   campaign_id="122089", post_ids=[str(1000 + i) for i in range(6)],
                   sort_type="custom"),
        Collection(id="1798655", title="| Femdom |", post_count=70,
                   campaign_id="122089", post_ids=[str(1000 + i) for i in range(6)],
                   sort_type="custom"),
        Collection(id="1793962", title="| Exclusive Videos |", post_count=59,
                   campaign_id="122089", post_ids=[str(1000 + i) for i in range(3)],
                   sort_type="custom"),
        Collection(id="2077477", title="Comic / Doujinshi / Vtuber Art", post_count=52,
                   campaign_id="122089", post_ids=[], sort_type="custom"),
    ]


def main() -> int:
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "data", "shots")
    os.makedirs(out_dir, exist_ok=True)

    from PySide6.QtCore import QCoreApplication, Qt, QTimer
    from PySide6.QtWidgets import QApplication, QTabWidget

    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv[:1])

    from patreon_dl.config import AppConfig
    from patreon_dl.downloader import Task
    from patreon_dl.models import Campaign
    from patreon_dl.ui.main_window import MainWindow
    from patreon_dl.ui.settings_dialog import SettingsDialog
    from patreon_dl.ui.theme import stylesheet

    config = AppConfig()
    window = MainWindow(config)
    window.resize(1500, 950)
    window.show()
    app.processEvents()

    posts = build_fake_posts()
    window._on_posts_batch(posts)
    window._on_fetch_completed(len(posts))
    app.processEvents()

    # 合集下拉框：填充 + 过滤
    window._on_collections_ready(build_fake_collections())
    app.processEvents()
    checks = [
        ("合集下拉框条目数 = 合集数 + 1（全部作品）",
         window.collection_combo.count() == 5),
        ("默认选中「全部作品」，表格显示全部作品",
         window._current_collection() is None and window.post_model.rowCount() == 6),
    ]
    # 选中「| Femdom |」（6 篇）后表格应只剩它包含的作品
    window._select_collection("1798655")
    app.processEvents()
    checks.append(("选中合集后表格被过滤",
                   window.post_model.rowCount() == 6
                   and window._selected_collection_ids() is not None))
    # 选中只有一个 post 的合集
    window._select_collection("1793962")
    app.processEvents()
    checks.append(("只有 3 篇的合集 -> 表格 3 行", window.post_model.rowCount() == 3))
    # 合集里没有的作品
    window._select_collection("2077477")
    app.processEvents()
    checks.append(("空 post_ids 的合集 -> 表格 0 行", window.post_model.rowCount() == 0))
    # 回到全部
    window.collection_combo.setCurrentIndex(0)
    app.processEvents()
    checks.append(("切回「全部作品」-> 6 行", window.post_model.rowCount() == 6))
    checks.append(("合集提示文字非空", bool(window.collection_hint.text())))

    for label, ok in checks:
        print(f"  {'✅' if ok else '❌'} {label}")
    if not all(ok for _label, ok in checks):
        print("❌ 合集下拉框测试未通过")
        return 1

    # 「只抓这一篇」的自动识别
    checks = []
    window.creator_edit.setText("https://www.patreon.com/BBebe/posts/exclusive-videos-141949666"
                                "?utm_medium=clipboard_copy&utm_source=copyLink")
    app.processEvents()
    checks.append(("单篇链接 -> 复选框可用", window.single_post_check.isEnabled()))
    checks.append(("单篇链接 -> 自动勾选", window.single_post_check.isChecked()))
    checks.append(("单篇链接 -> 识别出的作品 ID", window._detected_post_id == "141949666"))

    window.creator_edit.setText("https://www.patreon.com/cw/BBebe")
    app.processEvents()
    checks.append(("主页链接 -> 复选框禁用", not window.single_post_check.isEnabled()))
    checks.append(("主页链接 -> 自动取消勾选", not window.single_post_check.isChecked()))
    checks.append(("主页链接 -> 无作品 ID", window._detected_post_id is None))

    window.creator_edit.setText("https://www.patreon.com/c/rossdraws/posts/grasp-dark-171059589")
    app.processEvents()
    checks.append(("另一种单篇链接 -> 勾选", window.single_post_check.isChecked()
                   and window._detected_post_id == "171059589"))

    for label, ok in checks:
        print(f"  {'✅' if ok else '❌'} {label}")
    if not all(ok for _label, ok in checks):
        print("❌ 「只抓这一篇」自动识别测试未通过")
        return 1

    window.creator_edit.setText("https://www.patreon.com/cw/BBebe")
    app.processEvents()
    window.post_model.set_all_checked(True)
    window.post_table.selectRow(1)
    app.processEvents()

    campaign = Campaign(id="122089", name="Test Creator", vanity="test")
    task = Task(item=posts[0].media[0], post=posts[0], campaign=campaign,
                dest_dir="x", filename="a.jpg")
    window.queue_model.add_task(task)
    window.queue_model.update_progress(task, 500, 1000)
    window.queue_model.set_status(task, "已完成")
    window.log("界面冒烟测试")
    app.processEvents()

    written = []
    for theme in ("dark", "light"):
        window.setStyleSheet(stylesheet(theme))
        app.processEvents()
        QTimer.singleShot(50, app.quit)
        app.exec()
        path = os.path.join(out_dir, f"gui_{theme}.png")
        window.grab().save(path)
        written.append(path)

    dialog = SettingsDialog(config, window)
    dialog.resize(760, 660)
    dialog.show()
    app.processEvents()
    QTimer.singleShot(50, app.quit)
    app.exec()
    path = os.path.join(out_dir, "gui_settings.png")
    dialog.grab().save(path)
    written.append(path)

    # 「命名与归档」标签页：里面是模板与「按部分分目录」的开关
    tabs = dialog.findChild(QTabWidget)
    if tabs is not None:
        for index in range(tabs.count()):
            if "命名" in (tabs.tabText(index) or ""):
                tabs.setCurrentIndex(index)
                app.processEvents()
                QTimer.singleShot(50, app.quit)
                app.exec()
                path = os.path.join(out_dir, "gui_settings_naming.png")
                dialog.grab().save(path)
                written.append(path)
                break
    dialog.close()
    window.close()

    for path in written:
        print(f"  {os.path.getsize(path):>8} B  {path}")
    print("✅ 界面冒烟测试通过")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        raise SystemExit(1)
