"""界面配色与样式表。"""

from __future__ import annotations

DARK = {
    "bg": "#15171c",
    "bg_alt": "#1c1f26",
    "panel": "#20242c",
    "panel_alt": "#262b34",
    "border": "#333a46",
    "text": "#e6e9ef",
    "text_dim": "#98a1b0",
    "accent": "#ff5c5c",
    "accent_dim": "#c94545",
    "accent_text": "#ffffff",
    "ok": "#4ec98a",
    "warn": "#f0b34a",
    "err": "#ff6b6b",
    "sel": "#2f3644",
}

LIGHT = {
    "bg": "#f4f5f7",
    "bg_alt": "#ffffff",
    "panel": "#ffffff",
    "panel_alt": "#f0f2f5",
    "border": "#d5d9e0",
    "text": "#1d2026",
    "text_dim": "#666e7d",
    "accent": "#e6443f",
    "accent_dim": "#c43733",
    "accent_text": "#ffffff",
    "ok": "#1f9d63",
    "warn": "#b7791f",
    "err": "#d63b3b",
    "sel": "#e2e7f0",
}


def palette(theme: str) -> dict[str, str]:
    return LIGHT if (theme or "").lower() == "light" else DARK


def stylesheet(theme: str = "dark") -> str:
    """返回整份应用样式表。"""
    c = palette(theme)
    return f"""
    * {{
        font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "PingFang SC", sans-serif;
        font-size: 13px;
    }}
    QWidget {{
        background-color: {c['bg']};
        color: {c['text']};
    }}
    QMainWindow, QDialog {{
        background-color: {c['bg']};
    }}
    QToolBar {{
        background-color: {c['panel']};
        border: none;
        border-bottom: 1px solid {c['border']};
        spacing: 6px;
        padding: 6px 8px;
    }}
    QToolBar QLabel {{ color: {c['text_dim']}; }}
    QStatusBar {{
        background-color: {c['panel']};
        border-top: 1px solid {c['border']};
        color: {c['text_dim']};
    }}
    QMenuBar {{ background-color: {c['panel']}; }}
    QMenuBar::item:selected {{ background: {c['sel']}; }}
    QMenu {{
        background-color: {c['panel']};
        border: 1px solid {c['border']};
        padding: 4px;
    }}
    QMenu::item {{ padding: 6px 22px; border-radius: 4px; }}
    QMenu::item:selected {{ background: {c['sel']}; }}

    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
        background-color: {c['bg_alt']};
        border: 1px solid {c['border']};
        border-radius: 6px;
        padding: 5px 8px;
        selection-background-color: {c['accent']};
        selection-color: {c['accent_text']};
    }}
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
    QPlainTextEdit:focus, QTextEdit:focus {{
        border: 1px solid {c['accent']};
    }}
    QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
        color: {c['text_dim']};
        background-color: {c['panel_alt']};
    }}
    QComboBox::drop-down {{ border: none; width: 18px; }}
    QComboBox QAbstractItemView {{
        background-color: {c['panel']};
        border: 1px solid {c['border']};
        selection-background-color: {c['sel']};
    }}
    QPushButton {{
        background-color: {c['panel_alt']};
        border: 1px solid {c['border']};
        border-radius: 6px;
        padding: 6px 14px;
        color: {c['text']};
    }}
    QPushButton:hover {{ background-color: {c['sel']}; }}
    QPushButton:pressed {{ background-color: {c['border']}; }}
    QPushButton:disabled {{ color: {c['text_dim']}; background-color: {c['panel_alt']}; }}
    QPushButton[accent="true"] {{
        background-color: {c['accent']};
        border: 1px solid {c['accent']};
        color: {c['accent_text']};
        font-weight: 600;
    }}
    QPushButton[accent="true"]:hover {{ background-color: {c['accent_dim']}; }}
    QPushButton[accent="true"]:disabled {{
        background-color: {c['panel_alt']};
        border-color: {c['border']};
        color: {c['text_dim']};
    }}

    QTableView, QTreeView, QListView {{
        background-color: {c['bg_alt']};
        alternate-background-color: {c['panel_alt']};
        border: 1px solid {c['border']};
        border-radius: 6px;
        gridline-color: {c['border']};
        selection-background-color: {c['sel']};
        selection-color: {c['text']};
        outline: none;
    }}
    QTableView::item {{ padding: 3px 6px; }}
    QHeaderView::section {{
        background-color: {c['panel']};
        color: {c['text_dim']};
        border: none;
        border-right: 1px solid {c['border']};
        border-bottom: 1px solid {c['border']};
        padding: 6px 8px;
        font-weight: 600;
    }}
    QTableCornerButton::section {{ background-color: {c['panel']}; border: none; }}

    QTabWidget::pane {{
        border: 1px solid {c['border']};
        border-radius: 6px;
        top: -1px;
        background-color: {c['bg_alt']};
    }}
    QTabBar::tab {{
        background-color: {c['panel']};
        color: {c['text_dim']};
        border: 1px solid {c['border']};
        border-bottom: none;
        border-top-left-radius: 6px;
        border-top-right-radius: 6px;
        padding: 6px 16px;
        margin-right: 2px;
    }}
    QTabBar::tab:selected {{ background-color: {c['bg_alt']}; color: {c['text']}; }}
    QTabBar::tab:hover {{ color: {c['text']}; }}

    QGroupBox {{
        border: 1px solid {c['border']};
        border-radius: 6px;
        margin-top: 12px;
        padding-top: 10px;
        font-weight: 600;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
        color: {c['text_dim']};
    }}
    QCheckBox, QRadioButton {{ spacing: 7px; }}
    QCheckBox::indicator, QRadioButton::indicator {{
        width: 15px; height: 15px;
        border: 1px solid {c['border']};
        border-radius: 4px;
        background-color: {c['bg_alt']};
    }}
    QRadioButton::indicator {{ border-radius: 8px; }}
    QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
        background-color: {c['accent']};
        border-color: {c['accent']};
    }}
    QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {c['accent']}; }}

    QProgressBar {{
        background-color: {c['bg_alt']};
        border: 1px solid {c['border']};
        border-radius: 6px;
        text-align: center;
        height: 18px;
        color: {c['text']};
    }}
    QProgressBar::chunk {{ background-color: {c['accent']}; border-radius: 5px; }}

    QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; }}
    QScrollBar::handle:vertical {{
        background: {c['border']}; border-radius: 5px; min-height: 28px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {c['text_dim']}; }}
    QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 0; }}
    QScrollBar::handle:horizontal {{
        background: {c['border']}; border-radius: 5px; min-width: 28px;
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

    QSplitter::handle {{ background-color: {c['border']}; }}
    QSplitter::handle:horizontal {{ width: 3px; }}
    QSplitter::handle:vertical {{ height: 3px; }}

    QLabel[hint="true"] {{ color: {c['text_dim']}; }}
    QLabel[heading="true"] {{ font-size: 15px; font-weight: 700; }}
    QFrame[card="true"] {{
        background-color: {c['panel']};
        border: 1px solid {c['border']};
        border-radius: 8px;
    }}
    QToolTip {{
        background-color: {c['panel']};
        color: {c['text']};
        border: 1px solid {c['border']};
        padding: 4px 6px;
    }}
    """
