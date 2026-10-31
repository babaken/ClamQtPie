"""
画面設計書7章のテスト（ログ読込・フィルタ・CSV出力）
"""
from core.log_reader_service import LogReaderService


_SAMPLE_LOG = """\
2026-07-14 10:00:00,123 [INFO] アプリ起動シーケンス開始
2026-07-14 10:00:05,456 [ERROR] [E1001] ClamAVサービスに接続できません
2026-07-14 10:05:00,789 [WARNING] [E7001] メール通知の送信に失敗しました
2026-07-15 09:00:00,000 [ERROR] [E3001] ファイルの隔離に失敗しました
不正なフォーマットの行（無視されるべき）
"""


def test_read_entries_parses_valid_lines(tmp_path):
    log_path = tmp_path / "app.log"
    log_path.write_text(_SAMPLE_LOG, encoding="utf-8")
    service = LogReaderService(log_path)

    entries = service.read_entries()

    assert len(entries) == 4  # 不正な行は無視される
    assert entries[0].level == "INFO"
    assert entries[1].error_code == "E1001"
    assert entries[1].category == "スキャン"
    assert entries[2].category == "通知"
    assert entries[3].category == "隔離"


def test_read_entries_missing_file_returns_empty_list(tmp_path):
    service = LogReaderService(tmp_path / "not_exist.log")
    assert service.read_entries() == []


def test_filter_by_level(tmp_path):
    log_path = tmp_path / "app.log"
    log_path.write_text(_SAMPLE_LOG, encoding="utf-8")
    service = LogReaderService(log_path)
    entries = service.read_entries()

    filtered = service.filter_entries(entries, levels={"ERROR"})

    assert len(filtered) == 2
    assert all(e.level == "ERROR" for e in filtered)


def test_filter_by_keyword_matches_error_code(tmp_path):
    log_path = tmp_path / "app.log"
    log_path.write_text(_SAMPLE_LOG, encoding="utf-8")
    service = LogReaderService(log_path)
    entries = service.read_entries()

    filtered = service.filter_entries(entries, keyword="E1001")

    assert len(filtered) == 1
    assert filtered[0].error_code == "E1001"


def test_filter_by_date_range(tmp_path):
    from datetime import datetime

    log_path = tmp_path / "app.log"
    log_path.write_text(_SAMPLE_LOG, encoding="utf-8")
    service = LogReaderService(log_path)
    entries = service.read_entries()

    filtered = service.filter_entries(entries, date_from=datetime(2026, 7, 15))

    assert len(filtered) == 1
    assert filtered[0].error_code == "E3001"


def test_export_csv_writes_utf8_bom(tmp_path):
    log_path = tmp_path / "app.log"
    log_path.write_text(_SAMPLE_LOG, encoding="utf-8")
    service = LogReaderService(log_path)
    entries = service.read_entries()

    output_path = tmp_path / "export.csv"
    service.export_csv(entries, output_path)

    raw = output_path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM
    content = raw.decode("utf-8-sig")
    assert "日時,レベル,カテゴリ,メッセージ" in content
    assert "E1001" not in content  # メッセージ本文にはエラーコードが含まれないため
    assert "ClamAVサービスに接続できません" in content

def test_read_entries_includes_rotated_files_in_date_order(tmp_path):
    # レビュー指摘: 現在のapp.logだけを読んでいたため、日次のローテーション後は前日以前のログが出なかった
    (tmp_path / "app.log.2026-07-13").write_text(
        "2026-07-13 09:00:00,000 [INFO] 前々日\n", encoding="utf-8"
    )
    (tmp_path / "app.log.2026-07-14").write_text(
        "2026-07-14 09:00:00,000 [INFO] 前日\n", encoding="utf-8"
    )
    (tmp_path / "app.log.bak").write_text("2026-07-01 00:00:00,000 [INFO] 対象外\n", encoding="utf-8")
    (tmp_path / "app.log").write_text("2026-07-15 09:00:00,000 [INFO] 当日\n", encoding="utf-8")
    service = LogReaderService(tmp_path / "app.log")

    assert [e.message for e in service.read_entries()] == ["前々日", "前日", "当日"]


def test_read_entries_since_skips_older_rotated_files(tmp_path):
    from datetime import date

    for day in ("2026-07-01", "2026-07-13", "2026-07-14"):
        (tmp_path / f"app.log.{day}").write_text(f"{day} 09:00:00,000 [INFO] {day}\n", encoding="utf-8")
    (tmp_path / "app.log").write_text("2026-07-15 09:00:00,000 [INFO] 当日\n", encoding="utf-8")
    service = LogReaderService(tmp_path / "app.log")

    # ファイル名の日付の翌日未明の行が含まれ得るため、1日前のファイルまで読む
    assert [e.message for e in service.read_entries(since=date(2026, 7, 14))] == ["2026-07-13", "2026-07-14", "当日"]


def test_export_csv_neutralizes_formula_like_values(tmp_path):
    # 表計算ソフトで開いたときに、メッセージ（検知したファイル名などを含む）が数式として扱われないこと
    from datetime import datetime

    from core.log_reader_service import LogEntry

    service = LogReaderService(tmp_path / "app.log")
    messages = ["=HYPERLINK(\"x\")", "+1", "-1", "@SUM(A1)", "\tTAB", "通常のメッセージ"]
    entries = [
        LogEntry(datetime=datetime(2026, 10, 27, 12, 0, 0), level="WARNING", category="スキャン", message=m, error_code="")
        for m in messages
    ]
    output_path = tmp_path / "export.csv"
    service.export_csv(entries, output_path)

    import csv

    with open(output_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))[1:]
    assert [row[3] for row in rows] == ["'=HYPERLINK(\"x\")", "'+1", "'-1", "'@SUM(A1)", "'\tTAB", "通常のメッセージ"]
