"""
テスト仕様書3.5節: CFG-I01, CFG-I02 に対応（keyringはモック化）
"""
import pytest

from core.config_service import ConfigService
from core.exceptions import KeyringUnavailableError


def test_smtp_password_set_and_get(tmp_path, monkeypatch):
    # CFG-I01
    store: dict[tuple[str, str], str] = {}

    def fake_set_password(service, account, password):
        store[(service, account)] = password

    def fake_get_password(service, account):
        return store.get((service, account))

    monkeypatch.setattr("keyring.set_password", fake_set_password)
    monkeypatch.setattr("keyring.get_password", fake_get_password)

    config_path = tmp_path / "config.json"
    service = ConfigService(config_path)
    service.load()

    service.set_smtp_password("hunter2")
    assert service.get_smtp_password() == "hunter2"


def test_keyring_unavailable_raises_error(tmp_path, monkeypatch):
    # CFG-I02
    def fake_get_password_raises(service, account):
        raise RuntimeError("No recommended backend was available")

    monkeypatch.setattr("keyring.get_password", fake_get_password_raises)

    config_path = tmp_path / "config.json"
    service = ConfigService(config_path)
    service.load()

    with pytest.raises(KeyringUnavailableError):
        service.get_smtp_password()
