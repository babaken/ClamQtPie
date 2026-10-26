"""
画面全体の配色（暗色テーマ）とスタイルシート

参照: docs/screenshots_i18n.zip（画面レイアウトのサンプル）
- 背景は黒系、カードは濃い灰色、強調色は赤（ClamQtPieのアイコンの盾の色に合わせる）
- ボタンは既定が枠線のみ、主操作は variant="primary"（赤）、取り消せない操作は variant="danger"
- カードは QFrame#card（BaseScreen.begin_card()で作る）
"""
from __future__ import annotations

from PySide6.QtCore import QDir, QPointF, Qt, QTemporaryDir
from PySide6.QtGui import QColor, QImage, QPainter, QPalette, QPolygonF
from PySide6.QtWidgets import QApplication, QPushButton, QStyleFactory, QWidget

COLOR_BG = "#111111"
COLOR_SURFACE = "#1d1d1d"  # ヘッダー・サイドバー・表の見出し
COLOR_CARD = "#2a2a2a"
COLOR_CARD_ALT = "#242424"
COLOR_BORDER = "#3c3c3c"
COLOR_TEXT = "#f2f2f2"
COLOR_TEXT_MUTED = "#a8a8a8"
COLOR_ACCENT = "#c8203c"
COLOR_ACCENT_HOVER = "#dc3550"
COLOR_ACCENT_SOFT = "#4a2329"  # 左ナビの選択中の背景
COLOR_DANGER = "#9b1c1c"
COLOR_OK = "#22b573"
COLOR_WARN = "#e0a030"
COLOR_NG = "#e5484d"

STYLE_SHEET = f"""
QMainWindow, QDialog {{ background: {COLOR_BG}; }}
QWidget {{ color: {COLOR_TEXT}; }}
QToolTip {{ background: {COLOR_CARD}; color: {COLOR_TEXT}; border: 1px solid {COLOR_BORDER}; }}

/* ヘッダー・サイドバー */
QFrame#appHeader {{ background: {COLOR_SURFACE}; border-bottom: 1px solid {COLOR_BORDER}; }}
QLabel#appName {{ color: {COLOR_ACCENT}; font-size: 15pt; font-weight: bold; }}
QLabel#appSubtitle {{ color: {COLOR_TEXT}; font-size: 10pt; font-weight: bold; }}
QLabel#statusPill {{
    background: {COLOR_CARD}; border-radius: 12px; padding: 4px 12px; font-size: 9pt;
}}
QFrame#sidebar {{ background: {COLOR_SURFACE}; border-right: 1px solid {COLOR_BORDER}; }}
QListWidget#navList {{ background: transparent; border: none; outline: none; font-weight: bold; }}
QListWidget#navList::item {{ padding: 10px 8px; margin: 2px 8px; border-radius: 6px; }}
QListWidget#navList::item:hover {{ background: {COLOR_CARD}; }}
QListWidget#navList::item:selected {{ background: {COLOR_ACCENT_SOFT}; color: {COLOR_TEXT}; }}
QFrame#sidebarFooter {{ background: {COLOR_CARD_ALT}; border-top: 1px solid {COLOR_BORDER}; }}
QLabel#sidebarFooterTitle {{ font-size: 8pt; font-weight: bold; }}
QLabel#sidebarFooterDetail {{ font-size: 8pt; color: {COLOR_TEXT_MUTED}; }}
QStatusBar {{ background: {COLOR_SURFACE}; border-top: 1px solid {COLOR_BORDER}; }}
QStatusBar QLabel {{ color: {COLOR_TEXT_MUTED}; padding: 0 8px; }}

/* 画面タイトル・カード */
QScrollArea#screenScroll, QWidget#screenBody {{ background: {COLOR_BG}; border: none; }}
QLabel#screenTitle {{ font-size: 18pt; font-weight: bold; }}
QLabel#screenSubtitle {{ color: {COLOR_TEXT_MUTED}; }}
QFrame#card {{ background: {COLOR_CARD}; border: 1px solid {COLOR_BORDER}; border-radius: 8px; }}
QFrame#card[accent="true"] {{ border: 1px solid {COLOR_ACCENT}; }}
QLabel#cardTitle {{ font-size: 11pt; font-weight: bold; }}
QLabel#cardText, QLabel#mutedText {{ color: {COLOR_TEXT_MUTED}; }}
QLabel#heroTitle {{ font-size: 15pt; font-weight: bold; }}

/* ボタン */
QPushButton {{
    background: {COLOR_CARD}; border: 1px solid #5a5a5a; border-radius: 6px;
    padding: 6px 14px; font-weight: bold;
}}
QPushButton:hover {{ background: #353535; }}
QPushButton:pressed, QPushButton:checked {{ background: {COLOR_ACCENT_SOFT}; }}
QPushButton:disabled {{ color: #777777; border-color: #444444; background: #262626; }}
QPushButton[variant="primary"] {{ background: {COLOR_ACCENT}; border-color: {COLOR_ACCENT}; color: white; }}
QPushButton[variant="primary"]:hover {{ background: {COLOR_ACCENT_HOVER}; }}
QPushButton[variant="primary"]:disabled {{ background: #5a2a32; border-color: #5a2a32; color: #bbbbbb; }}
QPushButton[variant="danger"] {{ background: {COLOR_DANGER}; border-color: {COLOR_DANGER}; color: white; }}
QPushButton#hamburger {{ padding: 4px 10px; font-size: 12pt; }}

/* 入力 */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QTimeEdit, QListWidget, QTextEdit, QPlainTextEdit {{
    background: #1a1a1a; border: 1px solid #555555; border-radius: 4px; padding: 4px;
    selection-background-color: {COLOR_ACCENT};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QTextEdit:focus {{ border-color: {COLOR_ACCENT}; }}
/* 数値・日付の上下ボタン（入力欄の背景・枠線を指定したため、既定の描画では暗い背景に埋もれて見えなかった） */
QSpinBox, QDoubleSpinBox, QDateEdit, QTimeEdit {{ padding-right: 22px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button, QDateEdit::up-button, QTimeEdit::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button, QDateEdit::down-button, QTimeEdit::down-button {{
    subcontrol-origin: padding; width: 20px; background: #363636; border-left: 1px solid #555555;
}}
QSpinBox::up-button, QDoubleSpinBox::up-button, QDateEdit::up-button, QTimeEdit::up-button {{
    subcontrol-position: top right; border-top-right-radius: 3px; border-bottom: 1px solid #4a4a4a;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button, QDateEdit::down-button, QTimeEdit::down-button {{
    subcontrol-position: bottom right; border-bottom-right-radius: 3px;
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover, QDateEdit::up-button:hover, QTimeEdit::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover, QDateEdit::down-button:hover,
QTimeEdit::down-button:hover {{ background: #4a4a4a; }}
QSpinBox::up-button:pressed, QDoubleSpinBox::up-button:pressed, QDateEdit::up-button:pressed,
QTimeEdit::up-button:pressed, QSpinBox::down-button:pressed, QDoubleSpinBox::down-button:pressed,
QDateEdit::down-button:pressed, QTimeEdit::down-button:pressed {{ background: {COLOR_ACCENT_SOFT}; }}
QCheckBox, QRadioButton {{ spacing: 8px; }}
QProgressBar {{ background: #1a1a1a; border: 1px solid {COLOR_BORDER}; border-radius: 4px; text-align: center; }}
QProgressBar::chunk {{ background: {COLOR_ACCENT}; border-radius: 3px; }}

/* 一覧表 */
QTableWidget {{
    background: {COLOR_CARD}; alternate-background-color: {COLOR_CARD_ALT};
    border: 1px solid {COLOR_BORDER}; border-radius: 6px; gridline-color: {COLOR_BORDER};
    selection-background-color: {COLOR_ACCENT_SOFT}; selection-color: {COLOR_TEXT};
}}
QHeaderView::section {{
    background: {COLOR_SURFACE}; color: {COLOR_TEXT}; border: none;
    border-bottom: 1px solid {COLOR_BORDER}; padding: 6px 8px; font-weight: bold;
}}
QTableCornerButton::section {{ background: {COLOR_SURFACE}; border: none; }}

/* タブ・グループ */
QTabWidget::pane {{ border: 1px solid {COLOR_BORDER}; border-radius: 6px; background: {COLOR_CARD}; top: -1px; }}
QTabBar::tab {{
    background: {COLOR_SURFACE}; border: 1px solid {COLOR_BORDER}; padding: 6px 14px;
    border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px;
}}
QTabBar::tab:selected {{ background: {COLOR_CARD}; border-bottom-color: {COLOR_ACCENT}; }}
QGroupBox {{
    background: {COLOR_CARD}; border: 1px solid {COLOR_BORDER}; border-radius: 8px;
    margin-top: 22px; padding: 12px 10px 10px 10px;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 4px; padding: 0 4px; font-weight: bold; font-size: 11pt; }}
"""


def apply_theme(app: QApplication) -> None:
    """Fusionスタイル＋暗色パレット＋スタイルシートを適用する（OSのライト/ダーク設定に関わらず暗色）"""
    fusion = QStyleFactory.create("Fusion")
    if fusion is not None:
        app.setStyle(fusion)
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: COLOR_BG,
        QPalette.ColorRole.WindowText: COLOR_TEXT,
        QPalette.ColorRole.Base: "#1a1a1a",
        QPalette.ColorRole.AlternateBase: COLOR_CARD_ALT,
        QPalette.ColorRole.Text: COLOR_TEXT,
        QPalette.ColorRole.Button: COLOR_CARD,
        QPalette.ColorRole.ButtonText: COLOR_TEXT,
        QPalette.ColorRole.Highlight: COLOR_ACCENT,
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.ToolTipBase: COLOR_CARD,
        QPalette.ColorRole.ToolTipText: COLOR_TEXT,
        QPalette.ColorRole.PlaceholderText: "#808080",
        QPalette.ColorRole.Link: COLOR_ACCENT_HOVER,
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#777777"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#777777"))
    app.setPalette(palette)
    app.setStyleSheet(STYLE_SHEET + _spin_arrow_rules())


#: 上下ボタンの矢印の画像を置く一時フォルダ（アプリの終了まで保持し、終了時に削除される）
_arrow_dir: QTemporaryDir | None = None
#: 矢印の表示サイズ（論理ピクセル）。画像は高DPIでもぼやけないよう2倍の大きさで描く
_ARROW_WIDTH, _ARROW_HEIGHT = 9, 5


def _spin_arrow_rules() -> str:
    """数値・日付の上下ボタンの矢印を指定するスタイル

    上下ボタンの背景をスタイルシートで指定すると、Qtは矢印を描かなくなる（枠線で三角形を作る指定も
    四角形になる）。そのため三角形をQPainterで描いた画像を一時フォルダに書き、image: url(...) で指定する
    （アイコンと同じく画像ファイルは同梱しない）。画像を書けない場合は空文字（矢印なしのまま動作は続ける）。
    """
    global _arrow_dir
    _arrow_dir = QTemporaryDir()
    if not _arrow_dir.isValid():
        return ""
    paths = {}
    for name, color, up in (
        ("up", COLOR_TEXT, True),
        ("down", COLOR_TEXT, False),
        ("up_disabled", "#666666", True),
        ("down_disabled", "#666666", False),
    ):
        path = _arrow_dir.filePath(f"{name}.png")
        if not _draw_arrow(color, up).save(path, "PNG"):
            return ""
        paths[name] = QDir.fromNativeSeparators(path)
    widgets = ("QSpinBox", "QDoubleSpinBox", "QDateEdit", "QTimeEdit")

    def rule(sub: str, image: str, states: tuple[str, ...] = ("",)) -> str:
        selectors = ", ".join(f"{w}::{sub}{state}" for w in widgets for state in states)
        return f"{selectors} {{ image: url({image}); width: {_ARROW_WIDTH}px; height: {_ARROW_HEIGHT}px; }}\n"

    return "\n" + "".join([
        rule("up-arrow", paths["up"]),
        rule("down-arrow", paths["down"]),
        rule("up-arrow", paths["up_disabled"], (":disabled", ":off")),
        rule("down-arrow", paths["down_disabled"], (":disabled", ":off")),
    ])


def _draw_arrow(color: str, up: bool) -> QImage:
    width, height = _ARROW_WIDTH * 2, _ARROW_HEIGHT * 2
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    if up:
        points = [QPointF(0, height), QPointF(width, height), QPointF(width / 2, 0)]
    else:
        points = [QPointF(0, 0), QPointF(width, 0), QPointF(width / 2, height)]
    painter.drawPolygon(QPolygonF(points))
    painter.end()
    return image


def set_variant(button: QPushButton, variant: str) -> QPushButton:
    """ボタンの見た目を切り替える（"primary" / "danger" / ""）。設定済みのスタイルを再評価させる"""
    button.setProperty("variant", variant)
    _repolish(button)
    return button


def set_accent(widget: QWidget, accent: bool) -> None:
    """カードの枠を強調色にする（注意が必要な状態の表示用）"""
    widget.setProperty("accent", "true" if accent else "false")
    _repolish(widget)


def _repolish(widget: QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
