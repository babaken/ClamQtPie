"""
DetectionAggregator — 検知通知の集約

参照: 通知機能詳細仕様書3章
- 最初の検知から60秒固定ウィンドウで集約（デバウンスと異なりリセットしない）
- 50件（max_batch_size）を超えたら即座に打ち切って確定する
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class DetectionEvent:
    path: str
    virus_name: str
    detected_at: str
    #: 実際に隔離できたか（集約した通知の「対応状況」に使う）
    quarantined: bool = False


@dataclass
class _Batch:
    events: list[DetectionEvent] = field(default_factory=list)
    timer: threading.Timer | None = None


class DetectionAggregator:
    def __init__(
        self,
        window_sec: float,
        max_batch_size: int,
        on_batch_ready: Callable[[list[DetectionEvent]], None],
    ) -> None:
        self._window_sec = window_sec
        self._max_batch_size = max_batch_size
        self._on_batch_ready = on_batch_ready
        self._lock = threading.Lock()
        self._batch: _Batch | None = None

    def add(self, event: DetectionEvent) -> None:
        """検知イベントを追加する（3.1節）

        最初の検知でタイマー開始、以降は追加のみでタイマーはリセットしない。
        max_batch_size到達時は即座に確定する（3.2節の打ち切り閾値）。
        """
        with self._lock:
            if self._batch is None:
                self._batch = _Batch()
                timer = threading.Timer(self._window_sec, self._flush)
                timer.daemon = True
                self._batch.timer = timer
                timer.start()

            self._batch.events.append(event)

            if len(self._batch.events) >= self._max_batch_size:
                self._flush_locked()

    def _flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if self._batch is None:
            return
        if self._batch.timer is not None:
            self._batch.timer.cancel()
        events = self._batch.events
        self._batch = None
        if events:
            self._on_batch_ready(events)

    def cancel(self) -> None:
        with self._lock:
            if self._batch is not None and self._batch.timer is not None:
                self._batch.timer.cancel()
            self._batch = None
