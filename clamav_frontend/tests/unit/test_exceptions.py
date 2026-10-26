"""
テスト仕様書3.8節: ERR-U01, ERR-U02 に対応
"""
from core.exceptions import (
    AppError,
    ClamdConnectionError,
    ConfigWriteError,
)


def test_app_error_has_code_message_detail():
    err = ClamdConnectionError(detail="Connection refused")
    assert err.code == "E1001"
    assert "接続できません" in err.message
    assert err.detail == "Connection refused"


def test_app_error_code_can_be_overridden():
    err = AppError("カスタムメッセージ", code="E9999")
    assert err.code == "E9999"
    assert err.message == "カスタムメッセージ"


def test_subclass_is_instance_of_app_error():
    err = ConfigWriteError()
    assert isinstance(err, AppError)
    assert err.code == "E6001"


def test_str_representation_includes_code():
    err = ClamdConnectionError()
    assert "[E1001]" in str(err)
