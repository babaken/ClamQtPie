"""Python 3.15 の遅延 import 対策のテスト（core/lazy_import_fix.py）"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_PROJECT = Path(__file__).resolve().parents[2]


def test_thread_pool_is_usable_even_if_pyside6_was_loaded_first():
    # 配布版（PyInstaller）では PySide6 の runtime hook が本体より先に PySide6 を読み込む。
    # その順番でも、APScheduler が使う ThreadPoolExecutor が本物のクラスになり、起動の直後に落ちないこと
    code = (
        "import PySide6.QtCore\n"
        "from core.lazy_import_fix import reify_concurrent_futures\n"
        "reify_concurrent_futures()\n"
        "import concurrent.futures\n"
        "with concurrent.futures.ThreadPoolExecutor(1) as pool:\n"
        "    assert pool.submit(lambda: 42).result() == 42\n"
        "assert isinstance(concurrent.futures.ProcessPoolExecutor, type)\n"
        "from apscheduler.schedulers.background import BackgroundScheduler\n"
        "scheduler = BackgroundScheduler()\n"
        "scheduler.start()\n"
        "scheduler.shutdown(wait=False)\n"
        "print('ok')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=_PROJECT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
