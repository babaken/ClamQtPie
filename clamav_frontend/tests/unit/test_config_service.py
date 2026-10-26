"""
テスト仕様書3.5節: CFG-U01〜U06 に対応
"""
import json

import pytest

from core.config_model import ConfigModel
from core.config_service import ConfigService
from core.exceptions import ConfigWriteError


def test_creates_default_config_when_missing(tmp_path):
    # CFG-U01
    config_path = tmp_path / "config.json"
    service = ConfigService(config_path)

    model = service.load()

    assert config_path.exists()
    assert model.schema_version == 1
    assert model.general.autostart is True
    assert model.clamd.tcp_port == 3310


def test_missing_keys_are_filled_with_defaults(tmp_path):
    # CFG-U02
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"schema_version": 1, "general": {"autostart": False}}))

    service = ConfigService(config_path)
    model = service.load()

    assert model.general.autostart is False  # 指定値は維持
    assert model.general.start_minimized is True  # 欠損キーは既定値で補完
    assert model.clamd.tcp_port == 3310  # セクション自体が無くても既定値


def test_invalid_retention_days_is_auto_corrected(tmp_path, caplog):
    # CFG-U03
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"schema_version": 1, "logging": {"retention_days": 999, "level": "INFO"}})
    )

    service = ConfigService(config_path)
    model = service.load()

    assert model.logging.retention_days == 90


def test_empty_freshclam_path_is_auto_corrected(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"schema_version": 1, "update": {"freshclam_path": ""}}))

    model = ConfigService(config_path).load()

    assert model.update.freshclam_path == "freshclam"


def test_write_permission_error_raises_config_write_error(tmp_path, monkeypatch):
    # CFG-U04
    # 注: テスト実行がroot権限の場合、chmodによる書き込み不可設定は無視されてしまうため、
    # 環境に依存しない形でos.replace自体を失敗させてConfigWriteErrorへの変換を検証する。
    import core.config_service as config_service_module

    config_path = tmp_path / "config.json"
    service = ConfigService(config_path)

    def _raise_os_error(*args, **kwargs):
        raise OSError("permission denied (simulated)")

    monkeypatch.setattr(config_service_module.os, "replace", _raise_os_error)

    with pytest.raises(ConfigWriteError):
        service.save(ConfigModel())


def test_save_uses_atomic_write_no_leftover_tmp(tmp_path):
    # CFG-U05
    config_path = tmp_path / "config.json"
    service = ConfigService(config_path)
    service.save(ConfigModel())

    tmp_files = list(tmp_path.glob(".config_*"))
    assert tmp_files == []
    assert config_path.exists()


def test_future_schema_version_is_not_overwritten(tmp_path):
    # CFG-U06
    config_path = tmp_path / "config.json"
    future_data = {"schema_version": 999, "general": {"autostart": True}}
    config_path.write_text(json.dumps(future_data))
    original_mtime = config_path.stat().st_mtime_ns

    service = ConfigService(config_path)
    model = service.load()

    assert model.schema_version == 999
    # 上書き保存されていないこと（mtimeが変わっていない）
    assert config_path.stat().st_mtime_ns == original_mtime
