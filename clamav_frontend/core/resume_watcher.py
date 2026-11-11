"""
スリープからの復帰の検出（サービス化・Web API設計書 9章。段階 12-3c）

一定の間隔（30 秒）で時計を確かめ、前に確かめたときから実際の時刻が想定より大きく進んでいれば、
スリープ（または休止）から復帰したとみなして resumed を出す。OS の通知（Windows のサービスの電源イベント、
Linux の logind の D-Bus）を受け取るより単純で、Windows・Linux で同じ仕組みになり、依存も増えない。
検出は最大で間隔の分だけ遅れるが、取りこぼしたスケジュールを取り返すには十分。

時計を手で進めた・時刻合わせで大きく進んだときも「復帰」とみなすが、そのときに行うのは取りこぼしの確認だけで、
過ぎた回が無ければ何も実行しない。
"""
from __future__ import annotations

import time
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal

#: 時計を確かめる間隔（秒）
CHECK_INTERVAL_SEC = 30
#: 想定よりこれ以上（秒）進んでいれば、復帰したとみなす
RESUME_THRESHOLD_SEC = 120


class ResumeWatcher(QObject):
    #: 止まっていたと思われる秒数
    resumed = Signal(float)

    def __init__(self, clock: Callable[[], float] = time.time, interval_sec: int = CHECK_INTERVAL_SEC,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._clock = clock
        self._interval = interval_sec
        self._last = clock()
        self._timer = QTimer(self)
        self._timer.setInterval(interval_sec * 1000)
        self._timer.timeout.connect(self.check)

    def start(self) -> None:
        self._last = self._clock()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def check(self) -> None:
        now = self._clock()
        gap = now - self._last - self._interval
        self._last = now
        if gap >= RESUME_THRESHOLD_SEC:
            self.resumed.emit(gap)
