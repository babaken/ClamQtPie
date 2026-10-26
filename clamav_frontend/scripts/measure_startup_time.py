"""
起動時間計測スクリプト

参照: 非機能要件詳細2.5節「起動時間」
「通常時（マイグレーションなし）で3秒以内に完了しトレイアイコンが表示されることを目安とする」

使い方:
    QT_QPA_PLATFORM=offscreen python scripts/measure_startup_time.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _prepare_app_copy(target_dir: Path) -> Path:
    for name in ("app.py", "alembic.ini"):
        shutil.copy(_ROOT / name, target_dir / name)
    for name in ("alembic", "core", "db", "gui", "icons"):
        shutil.copytree(_ROOT / name, target_dir / name)
    return target_dir / "app.py"


def _measure_once(app_dir: Path, label: str) -> float:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(app_dir)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["LANG"] = "C.UTF-8"

    start = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "app.py"],
        cwd=app_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    elapsed = None
    deadline = start + 15
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        if "アプリ起動シーケンス完了" in line:
            elapsed = time.monotonic() - start
            break

    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()

    if elapsed is None:
        print(f"[{label}] 完了ログを検出できませんでした（タイムアウトまたは異常終了）")
        return -1.0

    print(f"[{label}] 起動シーケンス完了までの時間: {elapsed:.3f} 秒")
    return elapsed


def main() -> None:
    print("== 起動時間計測（非機能要件詳細2.5節） ==")
    print("目安: 通常時（マイグレーションなし）で3秒以内")
    print()

    with tempfile.TemporaryDirectory() as tmp:
        app_dir = Path(tmp)
        _prepare_app_copy(app_dir)

        # 1回目: 初回起動（DBマイグレーション適用あり）
        first = _measure_once(app_dir, "1回目（マイグレーション適用あり）")

        # 2回目: 通常起動（マイグレーション済み、config.json/app.db再利用）
        second = _measure_once(app_dir, "2回目（マイグレーションなし・通常起動）")

    print()
    print("== 結果まとめ ==")
    print(f"初回起動: {first:.3f} 秒")
    print(f"通常起動: {second:.3f} 秒 {'(目安3秒以内: 達成)' if 0 <= second <= 3.0 else '(目安3秒を超過、または計測失敗)'}")


if __name__ == "__main__":
    main()
