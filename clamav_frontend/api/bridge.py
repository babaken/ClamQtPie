"""
API のスレッドと Qt のメインスレッドの受け渡し（サービス化・Web API設計書 5章・8章）

- QtInvoker: uvicorn のスレッド（asyncio ループ）から、状態を変える処理や QObject を触る処理を
  メインスレッドで実行させ、結果を concurrent.futures.Future で受け取る。実行中かどうかの判定もメインスレッドで
  行うことで、GUI・ブラウザ・スケジュールからの同時操作の取り合いを防ぐ
- EventHub: どのスレッドからでもイベントを出せ、購読者（asyncio.Queue）へ配る。各イベントに連番の id を付け、
  直近 1,000 件を保持して、再接続した購読者に Last-Event-ID より後のものを再送できるようにする
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal, Slot

#: EventHub が保持する直近のイベント数（再送に使う）
EVENT_HISTORY_SIZE = 1000
#: 購読者ごとにためておけるイベント数。読み出しが追いつかない購読者は切り離す（メモリを使い続けないように）
SUBSCRIBER_QUEUE_SIZE = 1000


class QtInvoker(QObject):
    """メインスレッドで作る。call() は任意のスレッドから呼べる"""

    _requested = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # 受け取り側（このオブジェクト）はメインスレッドにあるため、別のスレッドからの emit はキューに入り、
        # メインスレッドのイベントループで _run が呼ばれる
        self._requested.connect(self._run)

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> concurrent.futures.Future:
        future: concurrent.futures.Future = concurrent.futures.Future()
        request = (future, fn, args, kwargs)
        if QThread.currentThread() is self.thread():
            # メインスレッドから呼ばれたときは、その場で実行する（キューに入れて待つと止まってしまう）
            self._run(request)
        else:
            self._requested.emit(request)
        return future

    async def run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """asyncio から使う形（await invoker.run(fn, ...)）"""
        return await asyncio.wrap_future(self.call(fn, *args, **kwargs))

    @Slot(object)
    def _run(self, request) -> None:
        future, fn, args, kwargs = request
        if not future.set_running_or_notify_cancel():
            return
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - 例外は呼び出し側に渡す
            future.set_exception(exc)
        else:
            future.set_result(result)


@dataclass(frozen=True)
class Event:
    id: int
    name: str
    data: dict = field(default_factory=dict)


#: 配信の終わりを知らせる印（Subscription.queue に入る。Event ではない）
END = object()


class Subscription:
    """1つの購読。queue からイベントを読み出す。closed になったら（読み出しが追いつかない等）接続を終える"""

    def __init__(self, hub: "EventHub", loop: asyncio.AbstractEventLoop, tag: str = "internal") -> None:
        self._hub = hub
        self.loop = loop
        #: どの待ち受けの購読か（"internal"・"web"。待ち受けを止めるときに、その待ち受けの購読だけを終わらせる）
        self.tag = tag
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self.closed = False

    def deliver(self, event: Event) -> None:
        """購読者のイベントループの中で呼ばれる"""
        if self.closed:
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.closed = True
            self._hub.unsubscribe(self)

    def close(self) -> None:
        self.closed = True
        self._hub.unsubscribe(self)

    def end(self) -> None:
        """配信を終える（待ち受けを止めるとき。任意のスレッドから呼べる）。読み出し側は END を受け取って終わる"""
        self.closed = True
        try:
            self.loop.call_soon_threadsafe(self._put_end)
        except RuntimeError:  # 購読者のイベントループが閉じている
            pass

    def _put_end(self) -> None:
        try:
            self.queue.put_nowait(END)
        except asyncio.QueueFull:
            pass  # 満杯なら、読み出し側は closed を見て終わる


def _first_event_id() -> int:
    """最初のイベントの番号。起動した時刻（ミリ秒）から始め、起動し直す前の番号より大きくする
    （トレイGUI は、つなぎ直すときに前のサービスで受け取った最後の番号を Last-Event-ID で送る。1 から数え直すと、
    その番号以下の新しいイベントが「送った分」として捨てられ、届かなかった。README_DEV §98）"""
    return int(time.time() * 1000)


class EventHub:
    def __init__(self, first_id: int | None = None) -> None:
        self._lock = threading.Lock()
        self._ids = itertools.count(_first_event_id() if first_id is None else first_id)
        self._latest_id = 0
        self._history: deque[Event] = deque(maxlen=EVENT_HISTORY_SIZE)
        self._subscribers: list[Subscription] = []

    def publish(self, name: str, data: dict | None = None) -> Event:
        """任意のスレッドから呼べる"""
        with self._lock:
            event = Event(next(self._ids), name, dict(data or {}))
            self._latest_id = event.id
            self._history.append(event)
            subscribers = list(self._subscribers)
        for subscription in subscribers:
            try:
                subscription.loop.call_soon_threadsafe(subscription.deliver, event)
            except RuntimeError:  # 購読者のイベントループが閉じている
                self.unsubscribe(subscription)
        return event

    def subscribe(self, tag: str = "internal") -> Subscription:
        """購読者のイベントループの中（async 関数の中）で呼ぶ"""
        subscription = Subscription(self, asyncio.get_running_loop(), tag)
        with self._lock:
            self._subscribers.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        with self._lock:
            if subscription in self._subscribers:
                self._subscribers.remove(subscription)

    @property
    def latest_id(self) -> int:
        """最後に出したイベントの番号（まだ無ければ 0）"""
        with self._lock:
            return self._latest_id

    def events_since(self, last_id: int) -> list[Event]:
        """last_id より後のイベント（再接続時の再送用）。保持している範囲のものだけ"""
        with self._lock:
            return [event for event in self._history if event.id > last_id]

    def end_all(self, tag: str | None = None) -> None:
        """購読の配信を終える（待ち受けを止める前に呼ぶ。つながったままの SSE を自分で終わらせ、
        uvicorn に打ち切らせない）。tag を指定したら、その待ち受けの購読だけ"""
        with self._lock:
            subscribers = [s for s in self._subscribers if tag is None or s.tag == tag]
            self._subscribers = [s for s in self._subscribers if s not in subscribers]
        for subscription in subscribers:
            subscription.end()

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)
