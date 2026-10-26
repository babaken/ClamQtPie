"""
ロギング基盤の動作確認（エラーコード一覧・例外設計書5.2節対応）
"""
from core.exceptions import ClamdConnectionError
from core.logger import log_app_error, setup_logger


def test_setup_logger_creates_log_file(tmp_path):
    log_dir = tmp_path / "logs"
    logger = setup_logger(log_dir, level="DEBUG", retention_days=30)

    logger.info("テストメッセージ")
    for h in logger.handlers:
        h.flush()

    assert (log_dir / "app.log").exists()
    content = (log_dir / "app.log").read_text(encoding="utf-8")
    assert "テストメッセージ" in content


def test_log_app_error_includes_error_code(tmp_path):
    log_dir = tmp_path / "logs"
    logger = setup_logger(log_dir, level="DEBUG", retention_days=30)

    err = ClamdConnectionError(detail="Connection refused")
    log_app_error(logger, err)
    for h in logger.handlers:
        h.flush()

    content = (log_dir / "app.log").read_text(encoding="utf-8")
    assert "[E1001]" in content
    assert "Connection refused" in content


def test_log_event_category_is_read_back_by_log_screen(tmp_path):
    # 業務ログ（検知・隔離等）はカテゴリ付きで記録され、ログ画面のカテゴリ列に反映される
    import logging

    from core.log_reader_service import LogReaderService
    from core.logger import CATEGORY_QUARANTINE, CATEGORY_SCAN, log_event

    log_dir = tmp_path / "logs"
    logger = setup_logger(log_dir, level="DEBUG", retention_days=30)
    log_event(CATEGORY_SCAN, "ウイルスを検知しました: C:\\a.txt (Eicar)", level=logging.WARNING)
    log_event(CATEGORY_QUARANTINE, "ファイルを隔離しました: C:\\a.txt", error_code="E3001")
    logger.info("カテゴリなしの行")
    for h in logger.handlers:
        h.flush()

    entries = LogReaderService(log_dir / "app.log").read_entries()
    assert [(e.level, e.category, e.error_code) for e in entries] == [
        ("WARNING", "スキャン", ""),
        ("INFO", "隔離", "E3001"),
        ("INFO", "システム", ""),
    ]
    assert entries[0].message == "ウイルスを検知しました: C:\\a.txt (Eicar)"


def test_invalid_level_falls_back_to_info(tmp_path):
    log_dir = tmp_path / "logs"
    logger = setup_logger(log_dir, level="NOT_A_LEVEL", retention_days=30)
    import logging

    assert logger.level == logging.INFO


def test_console_and_file_show_error_code_once_and_no_duplicate_lines(tmp_path, capsys):
    # Linux実機で発見: コンソールに「[ [E8004]]」と二重に表示され、同じ行がルートロガー経由で2回出ていた
    import logging

    from core.logger import CATEGORY_UPDATE, log_event

    logging.getLogger().addHandler(logging.StreamHandler())  # Alembicのlogging設定相当（ルートのコンソール出力）
    try:
        logger = setup_logger(tmp_path / "logs", level="INFO")
        log_event(CATEGORY_UPDATE, "定義DBの更新に失敗しました", level=logging.ERROR, error_code="E8004")
        for h in logger.handlers:
            h.flush()
    finally:
        logging.getLogger().handlers.pop()

    err = capsys.readouterr().err
    assert err.count("定義DBの更新に失敗しました") == 1
    assert "[E8004] <更新>" in err
    assert "[ [" not in err
    content = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "[ERROR] [E8004] <更新> 定義DBの更新に失敗しました" in content


def test_setup_logger_twice_closes_previous_file_handler(tmp_path):
    first = setup_logger(tmp_path / "a", level="INFO")
    old_handlers = list(first.handlers)
    setup_logger(tmp_path / "b", level="DEBUG")

    file_handlers = [h for h in old_handlers if hasattr(h, "baseFilename")]
    assert all(h.stream is None for h in file_handlers)  # 閉じられている


def _flush(logger):
    for h in logger.handlers:
        h.flush()


def test_clear_logs_empties_current_and_removes_old_files(tmp_path):
    # ログ画面の「ログをクリア」: app.logは書き込み中のため中身を空にし、日付付きの過去のファイルは削除する
    from core.logger import clear_logs

    log_dir = tmp_path / "logs"
    logger = setup_logger(log_dir, level="DEBUG", retention_days=30)
    logger.info("消える行")
    _flush(logger)
    (log_dir / "app.log.2026-10-18").write_text("古い行\n", encoding="utf-8")
    (log_dir / "app.log.2026-10-19").write_text("古い行\n", encoding="utf-8")
    (log_dir / "other.txt").write_text("対象外\n", encoding="utf-8")

    removed = clear_logs(log_dir)
    _flush(logger)

    assert removed == 2
    assert sorted(p.name for p in log_dir.iterdir()) == ["app.log", "other.txt"]
    content = (log_dir / "app.log").read_text(encoding="utf-8")
    assert "消える行" not in content
    assert "ログをクリアしました（過去のログファイル 2件を削除）" in content
    assert "<システム>" in content
    assert len(content.splitlines()) == 1


def test_logging_continues_after_clear(tmp_path):
    # 出力先（ファイル）を開いたまま空にするので、クリア後の行も同じファイルに続けて書かれる
    from core.logger import clear_logs

    log_dir = tmp_path / "logs"
    logger = setup_logger(log_dir, level="DEBUG", retention_days=30)
    logger.info("クリア前" * 50)
    _flush(logger)
    clear_logs(log_dir)
    logger.info("クリア後")
    _flush(logger)

    lines = (log_dir / "app.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[1].endswith("クリア後")
    assert "\x00" not in "".join(lines)  # 空にした位置から書かれている（先頭にNUL埋めが残らない）


def test_clear_logs_without_file_handler(tmp_path):
    # 出力先が別のファイルでも、指定フォルダのログは消去できる
    from core.logger import clear_logs

    log_dir = tmp_path / "other_logs"
    log_dir.mkdir()
    (log_dir / "app.log").write_text("古い行\n", encoding="utf-8")
    setup_logger(tmp_path / "logs", level="DEBUG", retention_days=30)

    assert clear_logs(log_dir) == 0
    assert (log_dir / "app.log").read_text(encoding="utf-8") == ""


import platform as _platform

import pytest


@pytest.mark.skipif(_platform.system() == "Windows", reason="POSIXのパーミッションのみ対象")
def test_log_dir_and_files_are_owner_only(tmp_path):
    # セキュリティレビュー: ログには検知パス・ウイルス名等が平文で入るため、他ユーザーに読まれないようにする
    import os
    import stat

    log_dir = tmp_path / "logs"
    logger = setup_logger(log_dir, level="INFO")
    logger.info("機密を含み得る行")
    _flush(logger)

    mode = lambda p: stat.S_IMODE(os.stat(p).st_mode)
    assert mode(log_dir) == 0o700
    assert mode(log_dir / "app.log") == 0o600
    assert not (mode(log_dir / "app.log") & (stat.S_IRGRP | stat.S_IROTH))


@pytest.mark.skipif(_platform.system() == "Windows", reason="POSIXのパーミッションのみ対象")
def test_existing_world_readable_logs_are_tightened(tmp_path):
    # 過去バージョンで作られた0644のログも、setup_logger時に0600へ直す
    import os
    import stat

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    old = log_dir / "app.log.2026-10-19"
    old.write_text("古い行\n")
    os.chmod(old, 0o644)
    os.chmod(log_dir, 0o755)

    setup_logger(log_dir, level="INFO")

    assert stat.S_IMODE(os.stat(log_dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(old).st_mode) == 0o600


@pytest.mark.skipif(_platform.system() == "Windows", reason="POSIXのパーミッションのみ対象")
def test_rotated_log_stays_owner_only(tmp_path):
    # ローテーションで作られる新ログ・退避される過去ログも0600であること
    import logging
    import os
    import stat

    log_dir = tmp_path / "logs"
    logger = setup_logger(log_dir, level="INFO")
    logger.info("ローテ前")
    handler = next(
        h for h in logger.handlers if isinstance(h, logging.handlers.TimedRotatingFileHandler)
    )
    handler.doRollover()
    logger.info("ローテ後")
    _flush(logger)

    mode = lambda p: stat.S_IMODE(os.stat(p).st_mode)
    assert mode(log_dir / "app.log") == 0o600
    for rotated in log_dir.glob("app.log.*"):
        assert mode(rotated) == 0o600
