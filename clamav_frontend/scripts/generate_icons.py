"""
アイコン素材生成スクリプト

ClamAV公式ロゴは商標のため使用・改変せず、オリジナルの図案で描画する。
  - 盾の中の二枚貝（clam）: ClamAVを利用していることを表す
  - 盾に弾かれる赤いウイルスと跳ね返りの軌跡: ウイルスを防いでいることを表す
  - トレイ用の状態バッジ（右下）: 監視中=緑チェック / 検知あり=橙「!」 / エラー=赤「×」

使い方（clamav_frontend/ で実行）:
    python scripts/generate_icons.py

出力:
    icons/tray_normal.png, tray_monitoring.png, tray_detection.png, tray_error.png（64px）
    icons/app.png（256px、プレビュー用）
    icons/clamQtPie.ico（16/24/32/48/64/128/256px、PyInstallerのexeアイコン）
"""
from __future__ import annotations

import math
import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)

_ROOT = Path(__file__).resolve().parent.parent
_ICONS_DIR = _ROOT / "icons"

_SHIELD_TOP = QColor("#1E88E5")
_SHIELD_BOTTOM = QColor("#0D47A1")
_SHELL_FILL = QColor("#FFF3E0")
_SHELL_LINE = QColor("#0D47A1")
_VIRUS = QColor("#E53935")
_VIRUS_DARK = QColor("#B71C1C")

#: 盾の中心X（サイズ比）。右上にウイルスを置く余白を確保するため左寄せにする
_SHIELD_CX = 0.38

_BADGES = {
    "monitoring": (QColor("#43A047"), "check"),
    "detection": (QColor("#FB8C00"), "exclaim"),
    "error": (QColor("#E53935"), "cross"),
}


def _shield_path(s: float) -> QPainterPath:
    """盾の輪郭（左寄せ。右上はウイルスの描画スペース）"""
    cx, top, w = _SHIELD_CX * s, 0.10 * s, 0.62 * s
    left, right = cx - w / 2, cx + w / 2
    path = QPainterPath()
    path.moveTo(cx, top)
    path.cubicTo(cx + 0.14 * s, top + 0.07 * s, right - 0.06 * s, top + 0.07 * s, right, top + 0.08 * s)
    path.cubicTo(right, 0.55 * s, cx + 0.20 * s, 0.80 * s, cx, 0.93 * s)
    path.cubicTo(cx - 0.20 * s, 0.80 * s, left, 0.55 * s, left, top + 0.08 * s)
    path.cubicTo(left + 0.06 * s, top + 0.07 * s, cx - 0.14 * s, top + 0.07 * s, cx, top)
    path.closeSubpath()
    return path


def _draw_shield(p: QPainter, s: float) -> None:
    grad = QLinearGradient(0, 0.10 * s, 0, 0.93 * s)
    grad.setColorAt(0, _SHIELD_TOP)
    grad.setColorAt(1, _SHIELD_BOTTOM)
    p.setPen(QPen(QColor("white"), 0.035 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.setBrush(QBrush(grad))
    p.drawPath(_shield_path(s))


def _draw_clam(p: QPainter, s: float) -> None:
    """扇形の二枚貝（ホタテ型）。上縁は波形、蝶番から放射状の筋"""
    hinge = QPointF(_SHIELD_CX * s, 0.70 * s)
    radius = 0.25 * s
    start_deg, end_deg, ribs = 25.0, 155.0, 6

    def point(deg: float, r: float) -> QPointF:
        rad = math.radians(deg)
        return QPointF(hinge.x() + r * math.cos(rad), hinge.y() - r * math.sin(rad))

    shell = QPainterPath()
    shell.moveTo(hinge)
    shell.lineTo(point(start_deg, radius))
    step = (end_deg - start_deg) / ribs
    for i in range(ribs):
        a0, a1 = start_deg + i * step, start_deg + (i + 1) * step
        shell.quadTo(point((a0 + a1) / 2, radius * 1.13), point(a1, radius))
    shell.closeSubpath()

    line_w = max(0.022 * s, 1.0)
    p.setPen(QPen(_SHELL_LINE, line_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.setBrush(_SHELL_FILL)
    p.drawPath(shell)

    if s >= 32:  # 小サイズでは筋を省略して潰れを防ぐ
        p.setPen(QPen(_SHELL_LINE, line_w * 0.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for i in range(1, ribs):
            p.drawLine(point(start_deg + i * step, radius * 0.18), point(start_deg + i * step, radius * 0.95))

    # 蝶番部分の「耳」
    p.setPen(QPen(_SHELL_LINE, line_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.setBrush(_SHELL_FILL)
    ear = QPainterPath()
    ear.moveTo(hinge.x() - 0.10 * s, hinge.y() + 0.005 * s)
    ear.lineTo(hinge.x() + 0.10 * s, hinge.y() + 0.005 * s)
    ear.lineTo(hinge.x() + 0.07 * s, hinge.y() + 0.07 * s)
    ear.lineTo(hinge.x() - 0.07 * s, hinge.y() + 0.07 * s)
    ear.closeSubpath()
    p.drawPath(ear)


def _draw_deflected_virus(p: QPainter, s: float) -> None:
    """盾の右上で弾かれるウイルスと、跳ね返りの軌跡"""
    center = QPointF(0.85 * s, 0.17 * s)
    body_r = 0.075 * s
    impact = QPointF(0.69 * s, 0.37 * s)  # 盾の右縁上の衝突点

    # 衝突点→ウイルス方向の単位ベクトルと、その法線
    dx, dy = center.x() - impact.x(), center.y() - impact.y()
    length = math.hypot(dx, dy)
    ux, uy = dx / length, dy / length
    nx, ny = -uy, ux

    # 衝突点の火花（盾の外側へ向かう黄色の放射線）
    if s >= 32:
        p.setPen(QPen(QColor("#FFC107"), 0.026 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for deg in (-40, 0, 40):
            rad = math.radians(deg)
            p.drawLine(
                QPointF(impact.x() + 0.03 * s * math.cos(rad), impact.y() - 0.03 * s * math.sin(rad)),
                QPointF(impact.x() + 0.08 * s * math.cos(rad), impact.y() - 0.08 * s * math.sin(rad)),
            )

    # 流線: ウイルスの後ろ（盾側）に引く短い平行線で「弾き飛ばされている」動きを表す
    lines = ((-0.035, 0.30, 0.55), (0.0, 0.25, 0.60), (0.035, 0.30, 0.55)) if s >= 32 else ((0.0, 0.25, 0.60),)
    p.setBrush(Qt.BrushStyle.NoBrush)
    for offset, t0, t1 in lines:
        a = QPointF(impact.x() + dx * t0 + nx * offset * s, impact.y() + dy * t0 + ny * offset * s)
        b = QPointF(impact.x() + dx * t1 + nx * offset * s, impact.y() + dy * t1 + ny * offset * s)
        p.setPen(QPen(QColor("white"), 0.05 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(a, b)
        p.setPen(QPen(_VIRUS, 0.024 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(a, b)

    # トゲ（スパイク）
    spikes = 8 if s >= 32 else 6
    p.setPen(QPen(_VIRUS_DARK, max(0.022 * s, 1.0), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.setBrush(_VIRUS_DARK)
    for i in range(spikes):
        rad = 2 * math.pi * i / spikes
        tip = QPointF(center.x() + body_r * 1.45 * math.cos(rad), center.y() + body_r * 1.45 * math.sin(rad))
        p.drawLine(center, tip)
        if s >= 32:
            p.drawEllipse(tip, 0.018 * s, 0.018 * s)

    p.setPen(QPen(QColor("white"), max(0.018 * s, 1.0)))
    p.setBrush(_VIRUS)
    p.drawEllipse(center, body_r, body_r)


def _draw_badge(p: QPainter, s: float, kind: str) -> None:
    color, mark = _BADGES[kind]
    center = QPointF(0.76 * s, 0.76 * s)
    r = 0.21 * s
    p.setPen(QPen(QColor("white"), 0.045 * s))
    p.setBrush(color)
    p.drawEllipse(center, r, r)

    pen = QPen(QColor("white"), 0.075 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    cx, cy = center.x(), center.y()
    if mark == "check":
        path = QPainterPath()
        path.moveTo(cx - 0.10 * s, cy + 0.005 * s)
        path.lineTo(cx - 0.025 * s, cy + 0.075 * s)
        path.lineTo(cx + 0.105 * s, cy - 0.075 * s)
        p.drawPath(path)
    elif mark == "exclaim":
        p.drawLine(QPointF(cx, cy - 0.11 * s), QPointF(cx, cy + 0.025 * s))
        p.drawPoint(QPointF(cx, cy + 0.10 * s))
    elif mark == "cross":
        d = 0.08 * s
        p.drawLine(QPointF(cx - d, cy - d), QPointF(cx + d, cy + d))
        p.drawLine(QPointF(cx - d, cy + d), QPointF(cx + d, cy - d))


def render(size: int, badge: str | None = None) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = float(size)
    _draw_shield(p, s)
    _draw_clam(p, s)
    if badge is None:
        _draw_deflected_virus(p, s)
    else:
        _draw_badge(p, s, badge)
    p.end()
    return image


def _png_bytes(image: QImage) -> bytes:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")
    buf.close()
    return bytes(data)


def write_ico(path: Path, sizes: list[int]) -> None:
    """PNG埋め込み形式の複数サイズICOを書き出す（Windows Vista以降で有効）"""
    blobs = [_png_bytes(render(sz)) for sz in sizes]
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    for sz, blob in zip(sizes, blobs):
        dim = 0 if sz >= 256 else sz  # 256pxは0で表す仕様
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    path.write_bytes(header + entries + b"".join(blobs))


def main() -> int:
    QGuiApplication.instance() or QGuiApplication(sys.argv)
    _ICONS_DIR.mkdir(exist_ok=True)

    # トレイの「通常」はウイルスを弾く図案、それ以外は状態バッジで区別する
    render(64).save(str(_ICONS_DIR / "tray_normal.png"))
    render(64, "monitoring").save(str(_ICONS_DIR / "tray_monitoring.png"))
    render(64, "detection").save(str(_ICONS_DIR / "tray_detection.png"))
    render(64, "error").save(str(_ICONS_DIR / "tray_error.png"))
    render(256).save(str(_ICONS_DIR / "app.png"))
    write_ico(_ICONS_DIR / "clamQtPie.ico", [16, 24, 32, 48, 64, 128, 256])

    for f in sorted(_ICONS_DIR.glob("*")):
        if f.suffix in (".png", ".ico"):
            print(f"{f.name}: {f.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
