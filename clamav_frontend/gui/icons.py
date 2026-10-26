"""
画面内で使う単色アイコン（左ナビ・カード）

画像ファイルやアイコンフォントを同梱せず、QPainterで描く（scripts/generate_icons.pyと同じ方針。
他製品のアイコン素材を流用しないため）。64x64の座標系で描き、要求サイズへ縮小する。
"""
from __future__ import annotations

import math
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

from gui.theme import COLOR_ACCENT

_CANVAS = 64.0


def _shield(p: QPainter) -> None:
    path = QPainterPath()
    path.moveTo(32, 6)
    path.lineTo(54, 14)
    path.cubicTo(54, 36, 46, 50, 32, 58)
    path.cubicTo(18, 50, 10, 36, 10, 14)
    path.closeSubpath()
    check = QPainterPath()
    check.moveTo(22, 32)
    check.lineTo(29, 39)
    check.lineTo(43, 24)
    p.fillPath(path, p.pen().color())
    p.save()
    p.setPen(QPen(QColor("#1d1d1d"), 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.drawPath(check)
    p.restore()


def _search(p: QPainter) -> None:
    p.drawEllipse(QPointF(27, 27), 15, 15)
    p.drawLine(QPointF(38, 38), QPointF(54, 54))


def _refresh(p: QPainter) -> None:
    rect = QRectF(12, 12, 40, 40)
    p.drawArc(rect, 60 * 16, 290 * 16)
    tip = QPointF(32 + 20 * math.cos(math.radians(60)), 32 - 20 * math.sin(math.radians(60)))
    arrow = QPainterPath()
    arrow.moveTo(tip.x() + 9, tip.y() - 9)
    arrow.lineTo(tip.x() + 9, tip.y() + 6)
    arrow.lineTo(tip.x() - 6, tip.y() + 6)
    arrow.closeSubpath()
    p.fillPath(arrow, p.pen().color())


def _history(p: QPainter) -> None:
    p.drawArc(QRectF(12, 12, 40, 40), 200 * 16, -310 * 16)
    p.drawLine(QPointF(32, 20), QPointF(32, 33))
    p.drawLine(QPointF(32, 33), QPointF(41, 39))
    arrow = QPainterPath()
    arrow.moveTo(6, 30)
    arrow.lineTo(18, 30)
    arrow.lineTo(12, 40)
    arrow.closeSubpath()
    p.fillPath(arrow, p.pen().color())


def _lock(p: QPainter) -> None:
    p.drawArc(QRectF(20, 8, 24, 28), 0, 180 * 16)
    p.drawLine(QPointF(20, 22), QPointF(20, 28))
    p.drawLine(QPointF(44, 22), QPointF(44, 28))
    body = QPainterPath()
    body.addRoundedRect(QRectF(13, 28, 38, 28), 5, 5)
    p.fillPath(body, p.pen().color())


def _gear(p: QPainter) -> None:
    path = QPainterPath()
    teeth = 8
    for i in range(teeth * 2):
        angle = math.pi * 2 * i / (teeth * 2)
        for offset in (-0.17, 0.17):
            radius = 26 if i % 2 == 0 else 19
            a = angle + offset
            point = QPointF(32 + radius * math.cos(a), 32 + radius * math.sin(a))
            if path.elementCount() == 0:
                path.moveTo(point)
            else:
                path.lineTo(point)
    path.closeSubpath()
    path.addEllipse(QPointF(32, 32), 8, 8)
    path.setFillRule(Qt.FillRule.OddEvenFill)
    p.fillPath(path, p.pen().color())


def _eye(p: QPainter) -> None:
    path = QPainterPath()
    path.moveTo(6, 32)
    path.quadTo(32, 6, 58, 32)
    path.quadTo(32, 58, 6, 32)
    p.drawPath(path)
    pupil = QPainterPath()
    pupil.addEllipse(QPointF(32, 32), 8, 8)
    p.fillPath(pupil, p.pen().color())


def _calendar(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(10, 14, 44, 40), 4, 4)
    band = QPainterPath()
    band.addRect(QRectF(10, 14, 44, 10))
    p.fillPath(band, p.pen().color())
    p.drawLine(QPointF(21, 8), QPointF(21, 16))
    p.drawLine(QPointF(43, 8), QPointF(43, 16))
    day = QPainterPath()
    day.addRect(QRectF(34, 36, 10, 10))
    p.fillPath(day, p.pen().color())


def _diagnostics(p: QPainter) -> None:
    path = QPainterPath()
    path.addRoundedRect(QRectF(8, 8, 48, 48), 6, 6)
    cross = QPainterPath()
    cross.addRect(QRectF(28, 18, 8, 28))
    cross.addRect(QRectF(18, 28, 28, 8))
    p.fillPath(path.subtracted(cross.simplified()), p.pen().color())


def _settings(p: QPainter) -> None:
    for y, knob_x in ((16, 22), (32, 42), (48, 28)):
        p.drawLine(QPointF(8, y), QPointF(56, y))
        knob = QPainterPath()
        knob.addEllipse(QPointF(knob_x, y), 6.5, 6.5)
        p.fillPath(knob, p.pen().color())


_DRAWERS: dict[str, Callable[[QPainter], None]] = {
    "home": _shield,
    "scan": _search,
    "update": _refresh,
    "log": _history,
    "quarantine": _lock,
    "engine": _gear,
    "realtime": _eye,
    "schedule": _calendar,
    "diagnostics": _diagnostics,
    "settings": _settings,
}

_cache: dict[tuple[str, int, str], QPixmap] = {}


def pixmap(name: str, size: int = 24, color: str = COLOR_ACCENT) -> QPixmap:
    """アイコン名（左ナビの画面キーと同じ）の画像。未定義の名前は空の画像"""
    key = (name, size, color)
    if key not in _cache:
        image = QPixmap(size, size)
        image.fill(Qt.GlobalColor.transparent)
        drawer = _DRAWERS.get(name)
        if drawer is not None:
            painter = QPainter(image)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.scale(size / _CANVAS, size / _CANVAS)
            painter.setPen(
                QPen(QColor(color), 5.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            )
            painter.setBrush(Qt.BrushStyle.NoBrush)
            drawer(painter)
            painter.end()
        _cache[key] = image
    return _cache[key]


def icon(name: str, color: str = COLOR_ACCENT) -> QIcon:
    result = QIcon()
    for size in (16, 24, 32, 48):
        result.addPixmap(pixmap(name, size, color))
    return result
