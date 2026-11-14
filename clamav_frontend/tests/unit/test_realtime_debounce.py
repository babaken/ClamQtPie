"""
リアルタイム監視（watchdog）のデバウンスのユニットテスト（README_DEV §97）

待ちは 1 本のスレッドでまとめて扱い、ファイルが一度に増えてもスレッドが増えないことを守る。
"""
import threading
import time

from core.realtime_manager import _DebounceManager


class _Recorder:
    def __init__(self) -> None:
        self.fired: list[str] = []
        self._lock = threading.Lock()
        self.event = threading.Event()

    def __call__(self, path: str) -> None:
        with self._lock:
            self.fired.append(path)
        self.event.set()

    def wait_count(self, n: int, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.fired) >= n:
                    return True
            time.sleep(0.01)
        return False


def test_many_paths_use_a_single_waiting_thread():
    # 500 件のファイルが一度に増えても、待ちのスレッドは 1 本だけ
    recorder = _Recorder()
    debounce = _DebounceManager(interval_sec=0.3, max_pending=1000, on_fire=recorder)
    before = threading.active_count()
    try:
        for i in range(500):
            debounce.notify(f"f{i}")
        assert threading.active_count() - before == 1
        assert recorder.wait_count(500)
        assert sorted(recorder.fired) == sorted(f"f{i}" for i in range(500))
    finally:
        debounce.cancel_all()


def test_repeated_events_for_one_path_fire_once_after_the_last_event():
    recorder = _Recorder()
    debounce = _DebounceManager(interval_sec=0.2, max_pending=10, on_fire=recorder)
    try:
        started = time.monotonic()
        for _ in range(5):
            debounce.notify("a")
            time.sleep(0.05)
        assert recorder.wait_count(1)
        assert time.monotonic() - started >= 0.2 + 0.05 * 4 - 0.02  # 最後のイベントから待つ
        time.sleep(0.3)
        assert recorder.fired == ["a"]
    finally:
        debounce.cancel_all()


def test_over_the_limit_fires_the_first_queued_path_at_once():
    # 上限を超えたら、最初に積んだパスを待たずに発火する。その後にイベントが続いても、最初に積んだ順は変わらない
    recorder = _Recorder()
    debounce = _DebounceManager(interval_sec=5.0, max_pending=2, on_fire=recorder)
    try:
        debounce.notify("old")
        debounce.notify("mid")
        debounce.notify("old")  # 書き込みが続くファイル
        debounce.notify("new")
        assert recorder.fired == ["old"]
        assert debounce.pending_count == 2
    finally:
        debounce.cancel_all()


def test_cancel_all_drops_waiting_paths_and_ends_the_thread():
    recorder = _Recorder()
    debounce = _DebounceManager(interval_sec=0.2, max_pending=10, on_fire=recorder)
    debounce.notify("a")
    thread = debounce._thread
    debounce.cancel_all()
    assert not thread.is_alive()
    debounce.notify("b")  # 止めた後のイベントは受け付けない
    time.sleep(0.4)
    assert recorder.fired == []


def test_failure_for_one_path_does_not_stop_the_others():
    fired = []

    def on_fire(path):
        if path == "bad":
            raise RuntimeError("boom")
        fired.append(path)

    debounce = _DebounceManager(interval_sec=0.05, max_pending=10, on_fire=on_fire)
    try:
        debounce.notify("bad")
        time.sleep(0.15)
        debounce.notify("good")
        deadline = time.monotonic() + 5
        while not fired and time.monotonic() < deadline:
            time.sleep(0.01)
        assert fired == ["good"]
    finally:
        debounce.cancel_all()
