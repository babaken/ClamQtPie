"""API とメインスレッドの受け渡し（QtInvoker・EventHub）のテスト（サービス化・Web API設計書 5章・8章）"""
from __future__ import annotations

import asyncio
import threading

import pytest

from api.bridge import EVENT_HISTORY_SIZE, SUBSCRIBER_QUEUE_SIZE, EventHub, QtInvoker


# -- QtInvoker -------------------------------------------------------------------
def test_call_from_another_thread_runs_on_main_thread(qapp, qtbot):
    invoker = QtInvoker()
    main_thread = threading.get_ident()
    futures = []

    def worker():
        futures.append(invoker.call(lambda a, b: (threading.get_ident(), a + b), 2, b=3))

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    qtbot.waitUntil(lambda: futures and futures[0].done(), timeout=5000)
    assert futures[0].result() == (main_thread, 5)


def test_exception_is_passed_back_to_the_caller(qapp, qtbot):
    invoker = QtInvoker()
    futures = []

    def fail():
        raise ValueError("だめ")

    t = threading.Thread(target=lambda: futures.append(invoker.call(fail)))
    t.start()
    t.join()
    qtbot.waitUntil(lambda: futures and futures[0].done(), timeout=5000)
    with pytest.raises(ValueError, match="だめ"):
        futures[0].result()


def test_call_from_main_thread_runs_immediately(qapp):
    # メインスレッドから呼んでも止まらない
    assert QtInvoker().call(lambda: 42).result(timeout=0) == 42


def test_run_can_be_awaited_from_asyncio_thread(qapp, qtbot):
    invoker = QtInvoker()
    main_thread = threading.get_ident()
    results = []

    def asyncio_thread():
        results.append(asyncio.run(invoker.run(threading.get_ident)))

    t = threading.Thread(target=asyncio_thread)
    t.start()
    qtbot.waitUntil(lambda: bool(results), timeout=5000)
    t.join()
    assert results == [main_thread]


def test_cancelled_request_is_not_run(qapp, qtbot):
    invoker = QtInvoker()
    called = []
    futures = []
    t = threading.Thread(target=lambda: futures.append(invoker.call(called.append, 1)))
    t.start()
    t.join()
    futures[0].cancel()  # メインスレッドが受け取る前に取り消す
    qapp.processEvents()
    assert called == []


# -- EventHub --------------------------------------------------------------------
def test_events_published_from_threads_reach_subscribers_in_order():
    hub = EventHub()

    async def main():
        subscription = hub.subscribe()
        threads = [threading.Thread(target=hub.publish, args=("scan.file", {"n": n})) for n in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        received = [await asyncio.wait_for(subscription.queue.get(), 5) for _ in range(5)]
        subscription.close()
        return received

    received = asyncio.run(main())
    assert [e.id for e in received] == sorted(e.id for e in received)
    assert sorted(e.data["n"] for e in received) == list(range(5))
    assert hub.subscriber_count == 0


def test_published_data_is_copied():
    # 出した側が後で辞書を書き換えても、配った内容は変わらない
    hub = EventHub()
    data = {"path": "a"}
    event = hub.publish("x", data)
    data["path"] = "b"
    assert event.data == {"path": "a"}


def test_events_since_returns_only_newer_kept_events():
    hub = EventHub(first_id=1)
    for n in range(EVENT_HISTORY_SIZE + 10):
        hub.publish("e", {"n": n})
    newer = hub.events_since(EVENT_HISTORY_SIZE + 5)
    assert [e.id for e in newer] == list(range(EVENT_HISTORY_SIZE + 6, EVENT_HISTORY_SIZE + 11))
    assert len(hub.events_since(0)) == EVENT_HISTORY_SIZE  # 保持するのは直近の分だけ


def test_slow_subscriber_is_dropped_without_affecting_others():
    hub = EventHub()

    async def main():
        slow = hub.subscribe()
        fast = hub.subscribe()
        received = []
        for n in range(SUBSCRIBER_QUEUE_SIZE + 1):
            hub.publish("e", {"n": n})
            await asyncio.sleep(0)  # 配達を進める
            received.append(await fast.queue.get())
        return slow, fast, received

    slow, fast, received = asyncio.run(main())
    assert slow.closed  # 読み出さない購読者は切り離す
    assert not fast.closed
    assert len(received) == SUBSCRIBER_QUEUE_SIZE + 1
    assert hub.subscriber_count == 1


def test_publish_after_subscriber_loop_closed_does_not_raise():
    hub = EventHub()

    async def main():
        return hub.subscribe()

    asyncio.run(main())  # ループはここで閉じる
    hub.publish("e")
    assert hub.subscriber_count == 0


def test_event_ids_start_above_the_ids_of_a_previous_start():
    # 起動し直す前のサービスで受け取った番号より、新しいサービスの番号が大きい（トレイGUI がつなぎ直したときに、
    # 新しいイベントを「送った分」として捨てられないように。README_DEV §98）
    import time

    before = int(time.time() * 1000)
    hub = EventHub()
    first = hub.publish("e")
    assert first.id >= before and hub.latest_id == first.id
    assert EventHub(first_id=1).latest_id == 0
