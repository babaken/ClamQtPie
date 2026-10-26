"""
フルスキャン性能検証スクリプト

参照: 非機能要件詳細2.1〜2.3節
「フルスキャン対象ファイル数: 数万ファイル（目安5万件）」
「スキャン結果のGUI更新間隔は最短200ms」「ファイル列挙は逐次処理でメモリ肥大化を防ぐ」

実clamdの代わりにモックclamdサーバ（即時OK応答）を使用し、本アプリ側の
オーバーヘッド（ファイル列挙・進捗シグナル発火・除外照合等）のみを計測する。
clamdエンジン自体のスキャン速度は計測対象外（別途実clamd環境での計測が必要）。

使い方:
    python scripts/measure_full_scan_performance.py [--files 50000]
"""
from __future__ import annotations

import argparse
import gc
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psutil

from core.clamav_service import ClamAVService
from core.config_model import ClamdConfig
from core.scan_dispatcher import ScanWorker


class _FastFakeClamdServer:
    """即時OK応答のみを返す軽量モックサーバ（性能計測のオーバーヘッドを避けるため専用実装）"""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(50)
        self.host, self.port = self._sock.getsockname()
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        self._sock.settimeout(0.2)
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            try:
                data = conn.recv(65536)
                if not data:
                    return
                text = data.decode(errors="replace")
                command_line = text.split("\0")[0]
                if command_line.lstrip("z").startswith("SCAN "):
                    path = command_line.lstrip("z")[len("SCAN "):]
                    conn.sendall(f"{path}: OK\x00".encode())
                else:
                    conn.sendall(b"PONG\x00")
            except OSError:
                pass

    def stop(self) -> None:
        self._stop = True
        self._sock.close()
        self._thread.join(timeout=2)


def _create_test_files(root: Path, count: int) -> None:
    print(f"テスト用ファイル {count} 件を作成中...")
    # サブディレクトリに分散させ、一般的なディレクトリ構造に近づける
    files_per_dir = 500
    idx = 0
    while idx < count:
        subdir = root / f"dir_{idx // files_per_dir:04d}"
        subdir.mkdir(exist_ok=True)
        for _ in range(min(files_per_dir, count - idx)):
            (subdir / f"file_{idx}.txt").write_text("dummy content")
            idx += 1
    print("作成完了")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", type=int, default=50000)
    args = parser.parse_args()

    print("== フルスキャン性能検証（非機能要件詳細2.1〜2.3節） ==")
    print(f"対象ファイル数: {args.files}件（基準値: 5万件）")
    print()

    server = _FastFakeClamdServer()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _create_test_files(root, args.files)

            config = ClamdConfig(
                connection_type="tcp",
                tcp_host=server.host,
                tcp_port=server.port,
                max_concurrent_connections=4,
            )
            clamav_service = ClamAVService(config)

            progress_events = []
            finished_summary = {}

            worker = ScanWorker(clamav_service, [str(root)], include_subfolders=True)
            worker.progress.connect(lambda scanned, total, path: progress_events.append(scanned))
            worker.finished.connect(lambda summary: finished_summary.update(summary))

            gc.collect()
            mem_before = psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)

            start = time.monotonic()
            worker.run()
            elapsed = time.monotonic() - start

            gc.collect()
            mem_after = psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)

        print()
        print("== 結果 ==")
        print(f"所要時間: {elapsed:.2f} 秒（{args.files / elapsed:.0f} ファイル/秒）")
        print(f"メモリ使用量: 開始前 {mem_before:.1f} MB → 終了後 {mem_after:.1f} MB "
              f"(増加 {mem_after - mem_before:.1f} MB)")
        print(f"進捗イベント発火回数: {len(progress_events)}")
        if finished_summary:
            print(f"スキャン結果サマリ: {finished_summary}")
    finally:
        server.stop()


if __name__ == "__main__":
    main()
