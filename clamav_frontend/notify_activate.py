"""
通知クリック用の補助プログラム（Windows）

通知（Toast）のクリックで clamqtpie:quarantine が開かれた時にWindowsから起動され、
起動中のアプリへ「隔離画面を開く」依頼ファイルを書いて終了する（core/activation.py）。

- ウィンドウを持たない（配布版はconsole=Falseのexe、開発時はpythonw.exeで実行）。
  PowerShellを使うとコンソールが一瞬表示されてしまうため、専用の補助プログラムにした（Windows実機テストで発見）
- 管理者権限を要求しない（本体のexeは常に管理者権限で起動するため、本体を直接起動するとUACが毎回出る）
- 起動を速くするため、PySide6等のアプリ本体のモジュールはimportしない
- 書き込む内容は固定（URLの内容は使わない）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

#: core/activation.py の user_state_dir() / REQUEST_FILE_NAME / SCREEN_QUARANTINE と同じ値（テストで一致を確認）。
#: 起動を速くするため core.version は import せず、フォルダ名（APP_SLUG）もここに書く
APP_SLUG = "clamqtpie"
REQUEST_FILE_NAME = "show_request"
SCREEN_QUARANTINE = "quarantine"


def request_file_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_SLUG / REQUEST_FILE_NAME


def main() -> int:
    path = request_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 一時ファイルに書いてから置き換え、アプリが書き込み途中の内容を読まないようにする
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(SCREEN_QUARANTINE, encoding="ascii")
        os.replace(tmp, path)
    except OSError:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
