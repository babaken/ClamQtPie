"""
利用者ごとの GUI の設定 gui.json（サービス化・Web API設計書 6.8節「設定の分割」。段階 12-3b）

サービスの設定（config.json、schema_version 2）から、自動起動・最小化などの利用者ごとの項目（general）を分けた。
- Windows: %APPDATA%\\ClamQtPie\\gui.json
- Linux: ~/.config/clamqtpie/gui.json（XDG_CONFIG_HOME があればその下）

段階 12-3b では、旧版のデータの移行（clamqtpie-ctl migrate）が書き出すだけ。トレイGUI が読むようになるのは段階 12-4
（それまで従来のアプリは、これまでどおり config.json の general を使う）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from core.config_model import GeneralConfig, _filtered
from core.version import APP_ID, APP_SLUG

GUI_SCHEMA_VERSION = 1


def gui_config_path(home: str | os.PathLike | None = None) -> Path:
    """gui.json の場所。home を渡すと、その利用者のホームの下（Linux で sudo から書き出すとき）"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_ID / "gui.json"
    if home is not None:
        return Path(home) / ".config" / APP_SLUG / "gui.json"
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / APP_SLUG / "gui.json"


def gui_settings_from(raw_general) -> dict:
    """旧版の config.json の general から gui.json の内容を作る（型の違う値は既定値にする）"""
    defaults = GeneralConfig()
    values = asdict(defaults)
    if isinstance(raw_general, dict):
        for name, value in _filtered(raw_general, GeneralConfig).items():
            if type(value) is type(getattr(defaults, name)):
                values[name] = value
    return {"schema_version": GUI_SCHEMA_VERSION, "general": values}


def write_gui_config(path: Path, data: dict) -> None:
    """一時ファイルに書いてから置き換える。既にあれば上書きしない（FileExistsError）"""
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".gui_", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
        raise


def read_gui_general(path: Path) -> GeneralConfig:
    """gui.json の general を読む（無い・読めない・型の違う値は既定値。トレイGUI。段階 12-4b）"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    general = data.get("general") if isinstance(data, dict) else None
    return GeneralConfig(**gui_settings_from(general)["general"])


def save_gui_general(path: Path, general: GeneralConfig) -> None:
    """gui.json の general を書き換える（一時ファイルに書いてから置き換える）"""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"schema_version": GUI_SCHEMA_VERSION, "general": asdict(general)}
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".gui_", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
        raise
