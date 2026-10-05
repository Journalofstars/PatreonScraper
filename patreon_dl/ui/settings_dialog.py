"""设置对话框。"""

from __future__ import annotations

import os

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import AppConfig

IMAGE_QUALITY_LABELS = [
    ("原图（最大，体积也最大）", "original"),
    ("大图（约 1080px）", "large"),
    ("中图（约 620px）", "medium"),
]


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("hint", True)
    label.setWordWrap(True)
    return label


class SettingsDialog(QDialog):
    """编辑 :class:`AppConfig`，点确定后写回。"""

    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.resize(720, 640)
        self.config = config
        self._build_ui()
        self._load_from_config()

    # ------------------------------------------------------------------ 构建
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        tabs = QTabWidget()
        tabs.addTab(self._tab_basic(), "基本")
        tabs.addTab(self._tab_content(), "下载内容")
        tabs.addTab(self._tab_naming(), "命名与归档")
        tabs.addTab(self._tab_behavior(), "行为与外观")
        layout.addWidget(tabs, 1)

        bottom = QHBoxLayout()
        reset = QPushButton("恢复默认设置")
        reset.clicked.connect(self._reset)
        bottom.addWidget(reset)
        bottom.addStretch(1)

        buttons = QDialogButtonBox()
        ok = buttons.addButton("确定", QDialogButtonBox.ButtonRole.AcceptRole)
        ok.setProperty("accent", True)
        cancel = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        ok.clicked.connect(self._apply)
        cancel.clicked.connect(self.reject)
        bottom.addWidget(buttons)
        layout.addLayout(bottom)

    # ---------------------------------------------------------------- 各标签页
    def _tab_basic(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setSpacing(10)

        box = QGroupBox("保存位置")
        form = QFormLayout(box)
        row = QHBoxLayout()
        self.output_edit = QLineEdit()
        row.addWidget(self.output_edit, 1)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse_output)
        row.addWidget(browse)
        holder = QWidget()
        holder.setLayout(row)
        form.addRow("下载目录", holder)
        form.addRow("", _hint("每个创作者会在该目录下建立一个子文件夹。"))
        outer.addWidget(box)

        box2 = QGroupBox("网络与性能")
        form2 = QFormLayout(box2)
        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 16)
        form2.addRow("同时下载数", self.workers_spin)
        self.delay_spin = QDoubleSpinBox()
        self.delay_spin.setRange(0.0, 10.0)
        self.delay_spin.setSingleStep(0.1)
        self.delay_spin.setSuffix(" 秒")
        form2.addRow("请求间隔", self.delay_spin)
        self.page_spin = QSpinBox()
        self.page_spin.setRange(5, 100)
        form2.addRow("每页抓取作品数", self.page_spin)
        self.max_posts_spin = QSpinBox()
        self.max_posts_spin.setRange(0, 100000)
        self.max_posts_spin.setSpecialValueText("不限制")
        form2.addRow("最多抓取作品数", self.max_posts_spin)
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(10, 600)
        self.timeout_spin.setSuffix(" 秒")
        form2.addRow("超时时间", self.timeout_spin)
        self.retries_spin = QSpinBox()
        self.retries_spin.setRange(0, 10)
        form2.addRow("失败重试次数", self.retries_spin)
        self.proxy_edit = QLineEdit()
        self.proxy_edit.setPlaceholderText("例如 http://127.0.0.1:7890，留空表示不使用")
        form2.addRow("代理服务器", self.proxy_edit)
        form2.addRow("", _hint("请求间隔越大越不容易被 Patreon 限流；并发过高也可能触发风控。"))
        outer.addWidget(box2)
        outer.addStretch(1)
        return page

    def _tab_content(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setSpacing(10)

        box = QGroupBox("要下载的内容类型")
        inner = QVBoxLayout(box)
        self.cb_images = QCheckBox("图片")
        self.cb_videos = QCheckBox("视频（含 mux 完整版）")
        self.cb_audio = QCheckBox("音频")
        self.cb_attachments = QCheckBox("附件 / 压缩包 / 源文件")
        self.cb_previews = QCheckBox("视频预览片段（30 秒试看，默认不下载）")
        self.cb_thumbnails = QCheckBox("视频封面图")
        self.cb_inline = QCheckBox("正文里内嵌的媒体（新版富文本作品）")
        for widget in (self.cb_images, self.cb_videos, self.cb_audio, self.cb_attachments,
                       self.cb_previews, self.cb_thumbnails, self.cb_inline):
            inner.addWidget(widget)
        inner.addWidget(_hint(
            "「正文里内嵌的媒体」指新版编辑器写进正文的图片/视频"
            "（常见于「索引贴」这类一篇里塞很多视频的作品）。"
            "这类媒体不在作品的关系字段里，<strong>必须逐个额外请求</strong>才能拿到，"
            "所以抓取会慢一些。默认开启——关掉的话这些内容会被漏掉。"
        ))
        outer.addWidget(box)

        box2 = QGroupBox("画质")
        form = QFormLayout(box2)
        self.quality_combo = QComboBox()
        for label, value in IMAGE_QUALITY_LABELS:
            self.quality_combo.addItem(label, value)
        form.addRow("图片画质", self.quality_combo)
        self.mux_check = QCheckBox("视频优先下载 mux 完整版（highest.mp4）")
        form.addRow("", self.mux_check)
        form.addRow("", _hint("关闭后只下载 Patreon 提供的直链视频（通常是比较短的预览）。"))
        outer.addWidget(box2)

        box3 = QGroupBox("外链视频（YouTube / Vimeo 等）")
        inner3 = QVBoxLayout(box3)
        self.cb_ytdlp = QCheckBox("使用 yt-dlp 下载外链视频")
        inner3.addWidget(self.cb_ytdlp)
        form3 = QFormLayout()
        self.ytdlp_edit = QLineEdit()
        self.ytdlp_edit.setPlaceholderText("yt-dlp 或完整路径，例如 C:\\tools\\yt-dlp.exe")
        form3.addRow("yt-dlp 路径", self.ytdlp_edit)
        self.ffmpeg_edit = QLineEdit()
        self.ffmpeg_edit.setPlaceholderText("留空则自动从 PATH 里查找 ffmpeg")
        form3.addRow("ffmpeg 路径", self.ffmpeg_edit)
        inner3.addLayout(form3)
        inner3.addWidget(_hint(
            "yt-dlp 需要自行安装并加入 PATH，否则请填写完整路径。<br>"
            "ffmpeg 用于把受保护视频的 HLS 分片无损封装成 MP4；"
            "没有它时会保存为 <code>.ts</code>（多数播放器仍可播放）。"
        ))
        outer.addWidget(box3)
        outer.addStretch(1)
        return page

    def _tab_naming(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setSpacing(10)

        box = QGroupBox("目录与文件名模板")
        form = QFormLayout(box)
        self.folder_edit = QLineEdit()
        form.addRow("创作者目录", self.folder_edit)
        self.name_edit = QLineEdit()
        form.addRow("作品目录", self.name_edit)
        self.file_edit = QLineEdit()
        form.addRow("文件名", self.file_edit)
        self.section_edit = QLineEdit()
        form.addRow("部分子目录", self.section_edit)
        form.addRow("", _hint(
            "可用变量：<br>"
            "创作者/作品目录：<code>{creator} {vanity} {campaign_id} {post_id} {title} "
            "{date} {datetime} {year} {month} {day} {post_type}</code><br>"
            "文件名：额外可用 <code>{index} {name} {ext} {kind} {media_id}</code>，"
            "例如 <code>{index:02d}_{name}</code><br>"
            "部分子目录：<code>{index} {title}</code>，例如 <code>{index:02d}_{title}</code>"
        ))
        outer.addWidget(box)

        box2 = QGroupBox("归档选项")
        inner = QVBoxLayout(box2)
        self.cb_group = QCheckBox("每篇作品单独建一个子目录")
        self.cb_split = QCheckBox("帖子内含多个部分时，每个部分单独建子目录")
        self.cb_meta = QCheckBox("保存 post.json（作品信息与媒体清单）")
        self.cb_text = QCheckBox("保存 post.txt（作品正文纯文本）")
        for widget in (self.cb_group, self.cb_split, self.cb_meta, self.cb_text):
            inner.addWidget(widget)
        inner.addWidget(_hint(
            "「帖子内含多个部分」指新版富文本作品里一篇塞了很多视频"
            "（例如「索引贴」）。开启后，程序会按正文里每个视频前面那段文字"
            "（标题或引用）给它们分组，每组放进一个子目录，"
            "命名规则由上面的「部分子目录」模板决定。<br>"
            "只有真正的多部分帖子会拆分；普通帖子不受影响。"
        ))
        outer.addWidget(box2)
        outer.addStretch(1)
        return page

    def _tab_behavior(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setSpacing(10)

        box = QGroupBox("增量下载")
        inner = QVBoxLayout(box)
        self.cb_skip = QCheckBox("跳过已经下载过的文件（增量同步）")
        self.cb_verify = QCheckBox("校验文件大小，不一致时重新下载")
        self.cb_open = QCheckBox("全部完成后自动打开下载目录")
        for widget in (self.cb_skip, self.cb_verify, self.cb_open):
            inner.addWidget(widget)
        inner.addWidget(_hint(
            "「已下载」按<strong>下载目录内的相对路径</strong>判断，而不是绝对路径："
            "把整个下载目录搬到别处、在这里改成新路径后，只要目录内的结构没变，"
            "就依然算已下载，不会重下。"
        ))
        outer.addWidget(box)

        box2 = QGroupBox("外观")
        form = QFormLayout(box2)
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("深色", "dark")
        self.theme_combo.addItem("浅色", "light")
        form.addRow("主题", self.theme_combo)
        outer.addWidget(box2)
        outer.addStretch(1)
        return page

    # ---------------------------------------------------------------- 读写
    def _load_from_config(self) -> None:
        c = self.config
        self.output_edit.setText(c.output_dir)
        self.workers_spin.setValue(int(c.max_workers))
        self.delay_spin.setValue(float(c.request_delay))
        self.page_spin.setValue(int(c.page_size))
        self.max_posts_spin.setValue(int(c.max_posts))
        self.timeout_spin.setValue(int(c.timeout))
        self.retries_spin.setValue(int(c.retries))
        self.proxy_edit.setText(c.proxy)

        self.cb_images.setChecked(c.download_images)
        self.cb_videos.setChecked(c.download_videos)
        self.cb_audio.setChecked(c.download_audio)
        self.cb_attachments.setChecked(c.download_attachments)
        self.cb_previews.setChecked(c.download_previews)
        self.cb_thumbnails.setChecked(c.download_thumbnails)
        self.cb_inline.setChecked(c.fetch_inline_media)
        index = self.quality_combo.findData(c.image_quality)
        self.quality_combo.setCurrentIndex(max(0, index))
        self.mux_check.setChecked(c.prefer_mux_full)
        self.cb_ytdlp.setChecked(c.use_yt_dlp)
        self.ytdlp_edit.setText(c.yt_dlp_path)
        self.ffmpeg_edit.setText(c.ffmpeg_path)

        self.folder_edit.setText(c.folder_template)
        self.name_edit.setText(c.name_template)
        self.file_edit.setText(c.file_template)
        self.section_edit.setText(c.section_template)
        self.cb_group.setChecked(c.group_by_post)
        self.cb_split.setChecked(c.split_sections)
        self.cb_meta.setChecked(c.write_metadata)
        self.cb_text.setChecked(c.write_post_text)

        self.cb_skip.setChecked(c.skip_existing)
        self.cb_verify.setChecked(c.verify_size)
        self.cb_open.setChecked(c.open_folder_when_done)
        index = self.theme_combo.findData(c.theme)
        self.theme_combo.setCurrentIndex(max(0, index))

    def _browse_output(self) -> None:
        start = self.output_edit.text() or os.path.expanduser("~")
        chosen = QFileDialog.getExistingDirectory(self, "选择下载目录", start)
        if chosen:
            self.output_edit.setText(chosen)

    def _reset(self) -> None:
        self.config = AppConfig()
        self._load_from_config()

    def _apply(self) -> None:
        c = self.config
        c.output_dir = self.output_edit.text().strip() or AppConfig.output_dir
        c.max_workers = self.workers_spin.value()
        c.request_delay = self.delay_spin.value()
        c.page_size = self.page_spin.value()
        c.max_posts = self.max_posts_spin.value()
        c.timeout = self.timeout_spin.value()
        c.retries = self.retries_spin.value()
        c.proxy = self.proxy_edit.text().strip()

        c.download_images = self.cb_images.isChecked()
        c.download_videos = self.cb_videos.isChecked()
        c.download_audio = self.cb_audio.isChecked()
        c.download_attachments = self.cb_attachments.isChecked()
        c.download_previews = self.cb_previews.isChecked()
        c.download_thumbnails = self.cb_thumbnails.isChecked()
        c.fetch_inline_media = self.cb_inline.isChecked()
        c.image_quality = self.quality_combo.currentData() or "original"
        c.prefer_mux_full = self.mux_check.isChecked()
        c.use_yt_dlp = self.cb_ytdlp.isChecked()
        c.yt_dlp_path = self.ytdlp_edit.text().strip() or "yt-dlp"
        c.ffmpeg_path = self.ffmpeg_edit.text().strip()

        c.folder_template = self.folder_edit.text().strip() or "{creator}"
        c.name_template = self.name_edit.text().strip() or "{date}_{title}"
        c.file_template = self.file_edit.text().strip() or "{index:02d}_{name}"
        c.section_template = self.section_edit.text().strip() or "{index:02d}_{title}"
        c.group_by_post = self.cb_group.isChecked()
        c.split_sections = self.cb_split.isChecked()
        c.write_metadata = self.cb_meta.isChecked()
        c.write_post_text = self.cb_text.isChecked()

        c.skip_existing = self.cb_skip.isChecked()
        c.verify_size = self.cb_verify.isChecked()
        c.open_folder_when_done = self.cb_open.isChecked()
        c.theme = self.theme_combo.currentData() or "dark"

        c.save()
        self.accept()
