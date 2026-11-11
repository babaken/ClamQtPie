"""
暗色テーマ（gui/theme.py）のテスト
"""
import re
from pathlib import Path

from PySide6.QtGui import QImage

from gui import theme


def test_spin_arrow_rules_point_to_drawn_images(qapp, tmp_path):
    # 数値・日付の上下ボタンの矢印が暗い背景に埋もれて見えなかった。QPainterで描いた三角形の画像を指定する
    rules = theme._spin_arrow_rules(tmp_path)

    urls = re.findall(r"url\(([^)]+)\)", rules)
    assert len(urls) == 4  # 上・下 × 通常・無効
    for url in urls:
        image = QImage(url)
        assert not image.isNull()
        assert Path(url).is_file()
    assert "QSpinBox::up-arrow" in rules and "QDateEdit::down-arrow:disabled" in rules


def test_arrow_image_is_a_triangle(qapp):
    image = theme._draw_arrow("#ffffff", up=True)
    width, height = image.width(), image.height()
    # 上向き: 下辺の中央は塗られ、上辺の両端は透明
    assert image.pixelColor(width // 2, height - 1).alpha() > 0
    assert image.pixelColor(0, 0).alpha() == 0
    assert image.pixelColor(width - 1, 0).alpha() == 0


def test_disabled_inputs_look_different():
    # 変えられない入力欄（保護された設定など）が、ほかの欄と同じ見た目だった（Linux 実機の確認での指摘）
    rule = re.search(r"QLineEdit:disabled[^{]*\{([^}]*)\}", theme.STYLE_SHEET)
    assert rule and "background" in rule.group(1) and "QComboBox:disabled" in rule.group(0)
    assert "QSpinBox:disabled" in rule.group(0)


def test_arrow_images_are_overwritten_in_one_place(qapp, tmp_path):
    # 起動のたびに一時フォルダを作り、終了時に消えずに溜まっていた（#55）。決まった場所に上書きで書く
    first = theme._spin_arrow_rules(tmp_path)
    second = theme._spin_arrow_rules(tmp_path)
    assert first == second
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "down.png", "down_disabled.png", "up.png", "up_disabled.png"]  # 書きかけのファイルも残らない


def test_arrow_image_dir_is_per_user_cache(monkeypatch, tmp_path):
    import sys

    from core.version import APP_SLUG

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    base = tmp_path / ("local" if sys.platform == "win32" else "cache")
    assert theme.arrow_image_dir() == base / APP_SLUG / "theme"


def test_no_arrows_when_the_folder_cannot_be_written(qapp, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert theme._spin_arrow_rules(blocker / "theme") == ""
