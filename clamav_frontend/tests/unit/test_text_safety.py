"""
外部から来る文字列の整形（core/text_safety.py）と、ログ・通知での利用のテスト

ファイル名などに制御文字（改行など）が含まれても、ログの1件の記録や通知の本文が別の行に分かれないこと。
"""
from core.logger import setup_logger
from core.text_safety import visible_controls


def test_control_characters_become_visible():
    assert visible_controls("a\nb\rc\td") == "a\\nb\\rc\\td"
    assert visible_controls("x\x1b[31my\x00z\x7f") == "x\\x1b[31my\\x00z\\x7f"
    assert visible_controls("p\u2028q\u2029r") == "p\\u2028q\\u2029r"


def test_backslashes_and_normal_text_are_kept_and_result_is_stable():
    path = r"C:\Users\taro-y\ダウンロード\file.exe"
    assert visible_controls(path) == path
    once = visible_controls("a\nb")
    assert visible_controls(once) == once


def test_log_record_with_line_break_stays_on_one_line(tmp_path):
    logger = setup_logger(tmp_path / "logs", logger_name="test_text_safety_logger")
    try:
        logger.warning("ウイルスを検知しました: %s", "/srv/share/a\n2026-10-27 00:00:00,000 [INFO] 別の記録")
        for handler in logger.handlers:
            handler.flush()
        lines = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert "a\\n2026-10-27" in lines[0]
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


def test_detection_notification_text_keeps_one_line_per_file():
    from unittest.mock import MagicMock

    from core.config_model import ConfigModel
    from core.notification_service import NotificationService

    config_service = MagicMock()
    config_service.current = ConfigModel()
    service = NotificationService(config_service)
    events = []
    service._aggregator = MagicMock(add=events.append)

    service.notify_detection("/srv/share/a\n/home/user/b", "Fake\nSignature", quarantined=False)

    assert events[0].path == "/srv/share/a\\n/home/user/b"
    assert events[0].virus_name == "Fake\\nSignature"
    assert "\n" not in service._desktop_body(events)
