"""
テスト仕様書3.3節: QT-I01（検知→隔離→一覧表示の一連の流れ）

ScanDispatcher（実クラス、fake_clamd_server使用）→ScanScreen→
QuarantineService（実クラス、一時DB使用）→QuarantineScreen まで、
実際のコンポーネントを結合して確認する。
"""
from core.clamav_service import ClamAVService
from core.config_model import ClamdConfig
from core.quarantine_service import QuarantineService
from core.scan_dispatcher import ScanDispatcher
from db.database import DatabaseService
from gui.screens.quarantine import QuarantineScreen
from gui.screens.scan import ScanScreen
from tests.conftest import FAKE_INFECTED_CONTENT


def test_detect_quarantine_list_flow(fake_clamd_server, tmp_path, qtbot, monkeypatch):
    # 検体ファイルを用意（判定はfake_clamd_serverの応答で決まるため中身はダミー）
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    eicar_path = target_dir / "eicar.txt"
    eicar_path.write_text(FAKE_INFECTED_CONTENT)

    fake_clamd_server.response_map["SCAN"] = f"{eicar_path}: Eicar-Test-Signature FOUND"

    # サービス層の用意
    clamd_config = ClamdConfig(
        connection_type="tcp", tcp_host=fake_clamd_server.host, tcp_port=fake_clamd_server.port
    )
    clamav_service = ClamAVService(clamd_config)
    dispatcher = ScanDispatcher(clamav_service)

    db_service = DatabaseService(tmp_path / "app.db")
    db_service.connect()
    quarantine_service = QuarantineService(db_service, tmp_path / "quarantine")

    # GUI結線
    scan_screen = ScanScreen()
    qtbot.addWidget(scan_screen)
    scan_screen.bind_services(dispatcher, quarantine_service)

    quarantine_screen = QuarantineScreen()
    qtbot.addWidget(quarantine_screen)
    quarantine_screen.bind_services(quarantine_service)

    # 1. スキャン実行（カスタム指定でtarget_dirを指定）
    scan_screen.custom_radio.setChecked(True)
    scan_screen.custom_path_list.addItem(str(target_dir))

    with qtbot.waitSignal(dispatcher.finished, timeout=5000):
        scan_screen.start_button.click()

    assert scan_screen.result_table.rowCount() == 1
    assert scan_screen.result_table.item(0, 0).text() == str(eicar_path)

    # 2. 検知結果を隔離
    import gui.screens.scan as scan_module

    monkeypatch.setattr(scan_module, "show_confirm_dialog", lambda *a, **k: True)
    scan_screen.result_table.selectRow(0)
    scan_screen.quarantine_button.click()

    assert not eicar_path.exists()  # 隔離によりファイルが移動している

    # 3. 隔離画面で一覧表示を確認
    quarantine_screen.reload()
    assert quarantine_screen.quarantine_table.rowCount() == 1
    assert quarantine_screen.quarantine_table.item(0, 0).text() == str(eicar_path)
    assert quarantine_screen.quarantine_table.item(0, 1).text() == "Eicar-Test-Signature"
    assert quarantine_screen.quarantine_table.item(0, 5).text() == "隔離中"

    # 4. 復元して元に戻ることも確認
    import gui.screens.quarantine as quarantine_module

    monkeypatch.setattr(quarantine_module, "show_confirm_dialog", lambda *a, **k: True)
    quarantine_screen.quarantine_table.selectRow(0)
    quarantine_screen.restore_button.click()
    assert eicar_path.exists()

    db_service.close()
