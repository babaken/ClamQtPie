"""
テスト仕様書3.6節: NT-U01, NT-U02 に対応
"""
import time

from core.detection_aggregator import DetectionAggregator, DetectionEvent


def _event(path="/tmp/a.txt", virus="Eicar-Test-Signature") -> DetectionEvent:
    return DetectionEvent(path=path, virus_name=virus, detected_at="2026-07-14T00:00:00")


def test_multiple_detections_within_window_aggregate_into_one_batch():
    # NT-U01
    batches = []
    aggregator = DetectionAggregator(window_sec=0.2, max_batch_size=50, on_batch_ready=batches.append)

    aggregator.add(_event("/tmp/a.txt"))
    time.sleep(0.05)
    aggregator.add(_event("/tmp/b.txt"))
    time.sleep(0.05)
    aggregator.add(_event("/tmp/c.txt"))

    time.sleep(0.3)  # ウィンドウ(0.2s)経過を待つ

    assert len(batches) == 1
    assert len(batches[0]) == 3


def test_max_batch_size_triggers_immediate_flush():
    # NT-U02
    batches = []
    aggregator = DetectionAggregator(window_sec=10.0, max_batch_size=3, on_batch_ready=batches.append)

    aggregator.add(_event("/tmp/a.txt"))
    aggregator.add(_event("/tmp/b.txt"))
    aggregator.add(_event("/tmp/c.txt"))  # 3件目で閾値到達、即座に確定するはず

    assert len(batches) == 1
    assert len(batches[0]) == 3


def test_new_window_starts_after_previous_flush():
    batches = []
    aggregator = DetectionAggregator(window_sec=0.2, max_batch_size=50, on_batch_ready=batches.append)

    aggregator.add(_event("/tmp/a.txt"))
    time.sleep(0.3)
    aggregator.add(_event("/tmp/b.txt"))
    time.sleep(0.3)

    assert len(batches) == 2
    assert len(batches[0]) == 1
    assert len(batches[1]) == 1


def test_cancel_prevents_pending_flush():
    batches = []
    aggregator = DetectionAggregator(window_sec=0.2, max_batch_size=50, on_batch_ready=batches.append)

    aggregator.add(_event("/tmp/a.txt"))
    aggregator.cancel()
    time.sleep(0.3)

    assert batches == []
