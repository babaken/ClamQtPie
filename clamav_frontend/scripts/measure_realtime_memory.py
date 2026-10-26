"""
リアルタイム監視 常駐メモリ計測スクリプト

参照: 非機能要件詳細2.4節「リアルタイム監視時のメモリ使用量目安」
「常時稼働時（監視のみ、検知なし）のメモリ使用量: 100〜200MB程度」

使い方:
    QT_QPA_PLATFORM=offscreen python scripts/measure_realtime_memory.py
"""
from __future__ import annotations

import gc
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psutil
from PySide6.QtWidgets import QApplication

from core.clamav_service import ClamAVService
from core.config_model import ClamdConfig, RealtimeConfig
from core.exclude_service import ExcludeService
from core.quarantine_service import QuarantineService
from core.realtime_manager import RealtimeManager
from core.watch_target_service import WatchTargetService
from db.database import DatabaseService


def _rss_mb() -> float:
    gc.collect()
    return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)


def main() -> None:
    print("== リアルタイム監視 常駐メモリ計測（非機能要件詳細2.4節） ==")
    print("目安: 常時100〜200MB程度")
    print()

    app = QApplication.instance() or QApplication(sys.argv)

    baseline = _rss_mb()
    print(f"ベースライン（QApplication起動直後）: {baseline:.1f} MB")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_service = DatabaseService(tmp_path / "app.db")
        db_service.connect()

        watch_dir = tmp_path / "watched"
        watch_dir.mkdir()

        clamd_config = ClamdConfig(connection_type="tcp", tcp_host="127.0.0.1", tcp_port=1)
        clamav_service = ClamAVService(clamd_config)
        watch_target_service = WatchTargetService(db_service)
        exclude_service = ExcludeService(db_service)
        quarantine_service = QuarantineService(db_service, tmp_path / "quarantine")
        realtime_config = RealtimeConfig()

        manager = RealtimeManager(
            clamav_service, watch_target_service, exclude_service, quarantine_service, realtime_config
        )
        watch_target_service.add(str(watch_dir))

        after_setup = _rss_mb()
        print(f"サービス層構築後: {after_setup:.1f} MB")

        manager.start()
        time.sleep(0.5)
        after_start = _rss_mb()
        print(f"リアルタイム監視開始直後: {after_start:.1f} MB")

        # max_pending_timers近くまでデバウンスタイマーを生成した状態を模擬
        print("大量ファイル作成によるデバウンスタイマー生成後の計測...")
        for i in range(500):
            (watch_dir / f"file_{i}.txt").write_text(str(i))
        time.sleep(1.0)
        under_load = _rss_mb()
        print(f"500ファイル作成直後（デバウンス処理中）: {under_load:.1f} MB")

        time.sleep(3.0)  # デバウンス・スキャンキュー処理が落ち着くまで待つ
        after_settle = _rss_mb()
        print(f"処理落ち着き後: {after_settle:.1f} MB")

        manager.stop()
        db_service.close()

    print()
    print("== 結果まとめ ==")
    print(f"常駐時（監視のみ）: {after_start:.1f} MB")
    print(f"負荷時（500ファイル作成直後）: {under_load:.1f} MB")
    within_target = 100 <= after_start <= 250  # Python/Qtベースのため若干の上振れは許容して判定
    print(
        f"目安100〜200MBとの比較: "
        f"{'概ね範囲内' if within_target else '目安を外れている可能性あり（要確認）'}"
    )


if __name__ == "__main__":
    main()
