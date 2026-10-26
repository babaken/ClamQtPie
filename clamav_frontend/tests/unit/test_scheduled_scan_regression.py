"""
Phase10で発見したapp.py内の回帰バグの再発防止テスト:
_run_scheduled_scan() が ScanWorker を誤った引数名（recursive=）で
呼び出しており、実際にスケジュールジョブが実行されるまで検出されなかった。
app.py と同一のシグネチャでScanWorkerを構築できることを確認する。
"""
from core.clamav_service import ClamAVService
from core.config_model import ClamdConfig
from core.exclude_service import ExcludeService
from core.scan_dispatcher import ScanWorker
from db.database import DatabaseService


def test_scan_worker_can_be_constructed_like_scheduled_scan_closure(
    fake_clamd_server, tmp_path
):
    """app.py の _run_scheduled_scan() と同一の呼び出し方でScanWorkerを構築・実行できること"""
    fake_clamd_server.response_map["SCAN"] = "dummy: OK"

    db_service = DatabaseService(tmp_path / "app.db")
    db_service.connect()
    exclude_service = ExcludeService(db_service)

    config = ClamdConfig(connection_type="tcp", tcp_host=fake_clamd_server.host, tcp_port=fake_clamd_server.port)
    clamav_service = ClamAVService(config)

    target_file = tmp_path / "target.txt"
    target_file.write_text("dummy")

    # app.py _run_scheduled_scan() と同一の呼び出し形（include_subfolders=、recursive=ではない）
    worker = ScanWorker(
        clamav_service, [str(target_file)], include_subfolders=True, exclude_service=exclude_service
    )
    worker.run()  # TypeErrorが発生しないことを確認

    db_service.close()
