"""
暗色テーマ（gui/theme.py）のテスト
"""
import re
from pathlib import Path

from PySide6.QtGui import QImage

from gui import theme


def test_spin_arrow_rules_point_to_drawn_images(qapp):
    # 数値・日付の上下ボタンの矢印が暗い背景に埋もれて見えなかった。QPainterで描いた三角形の画像を指定する
    rules = theme._spin_arrow_rules()

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
