"""
実clamonacc + 実clamdでのLinuxリアルタイム監視結合テスト（Phase 11）

実行条件（満たさない場合はスキップ）:
  - Linux、root権限（fanotifyのため。rootならpkexecを介さず監視シェルを直接起動する）
  - clamonaccがインストール済みで、clamdがLocalSocket(/var/run/clamav/clamd.ctl)で稼働中
  - clamd.confに OnAccessExcludeUname clamav 推奨

実行例（WSL/Linux）:
    sudo QT_QPA_PLATFORM=offscreen <venv>/bin/python -m pytest tests/integration/test_real_clamonacc.py

pkexecの認証失敗経路（一般ユーザー・認証エージェント無し）は CLAMQT_TEST_PKEXEC_NO_AGENT=1 で有効化する
（認証エージェントがある環境では実際の認証ダイアログが表示されてしまうため既定では実行しない）。
"""
import os
import platform
import shutil
from pathlib import Path

import pytest

from core.clamonacc_manager import ClamonaccRealtimeManager
from core.config_model import RealtimeConfig
from core.exclude_service import ExcludeService
from core.quarantine_service import QuarantineService
from core.watch_target_service import WatchTargetService
from db.database import DatabaseService
from tests.conftest import EICAR_CONTENT

_IS_LINUX = platform.system() == "Linux"
_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
_HAS_CLAMONACC = shutil.which("clamonacc") is not None
_CLAMD_SOCKET = Path("/var/run/clamav/clamd.ctl")

requires_real_clamonacc = pytest.mark.skipif(
    not (_IS_LINUX and _IS_ROOT and _HAS_CLAMONACC and _CLAMD_SOCKET.exists()),
    reason="Linux・root権限・clamonacc・clamd(LocalSocket)が揃っていないためスキップ",
)


@pytest.fixture
def services(tmp_path):
    db_service = DatabaseService(tmp_path / "app.db")
    db_service.connect()
    yield {
        "watch_target_service": WatchTargetService(db_service),
        "exclude_service": ExcludeService(db_service),
        "quarantine_service": QuarantineService(db_service, tmp_path / "quarantine"),
    }
    db_service.close()


def _build(services, tmp_path, **config_overrides):
    return ClamonaccRealtimeManager(
        services["watch_target_service"],
        services["exclude_service"],
        services["quarantine_service"],
        RealtimeConfig(**config_overrides),
        work_dir=tmp_path / "clamonacc",
        extra_exclude_dirs=[tmp_path / "quarantine", tmp_path / "clamonacc"],
    )


def _touch_until(qtbot, path: Path, predicate, timeout_ms: int) -> None:
    """clamonaccの監視開始を待ちつつ、ファイルを繰り返し開いてオンアクセススキャンを発生させる"""

    def _check():
        if path.exists():
            try:
                path.read_bytes()
            except OSError:
                pass  # OnAccessPrevention有効時はopenが拒否される
        assert predicate()

    qtbot.waitUntil(_check, timeout=timeout_ms)


@requires_real_clamonacc
def test_real_clamonacc_detects_and_quarantines_eicar(services, tmp_path, qtbot):
    watched = tmp_path / "watched"
    watched.mkdir()
    services["watch_target_service"].add(str(watched))
    manager = _build(services, tmp_path)

    detected = []
    errors = []
    manager.detected.connect(lambda p, v: detected.append((p, v)))
    manager.scan_error.connect(errors.append)

    assert manager.start() is True
    try:
        eicar = watched / "eicar.txt"
        eicar.write_text(EICAR_CONTENT)
        _touch_until(qtbot, eicar, lambda: bool(detected) or bool(errors), timeout_ms=60000)

        assert errors == []
        path, virus = detected[0]
        assert path == str(eicar)
        assert "Eicar" in virus
        # 隔離はファイル移動→DB登録の順のため、DB登録の完了まで待つ
        qtbot.waitUntil(lambda: len(services["quarantine_service"].list_all()) == 1, timeout=10000)
        assert not eicar.exists()
    finally:
        manager.stop()

    assert manager.is_running is False


@requires_real_clamonacc
def test_real_clamonacc_respects_exclude_folder(services, tmp_path, qtbot):
    watched = tmp_path / "watched"
    excluded = watched / "excluded"
    excluded.mkdir(parents=True)
    services["watch_target_service"].add(str(watched))
    services["exclude_service"].add("folder", str(excluded))
    manager = _build(services, tmp_path, default_action="notify_only")

    detected = []
    manager.detected.connect(lambda p, v: detected.append(p))
    assert manager.start() is True
    try:
        skipped = excluded / "eicar.txt"
        skipped.write_text(EICAR_CONTENT)
        sentinel = watched / "eicar.txt"  # 除外外のファイルで監視が有効になったことを確認する
        sentinel.write_text(EICAR_CONTENT)

        _touch_until(qtbot, sentinel, lambda: str(sentinel) in detected, timeout_ms=60000)
        for _ in range(3):
            skipped.read_bytes()
            qtbot.wait(1000)

        assert str(skipped) not in detected
    finally:
        manager.stop()


@requires_real_clamonacc
def test_real_clamonacc_stop_leaves_no_clamonacc_process(services, tmp_path, qtbot):
    watched = tmp_path / "watched"
    watched.mkdir()
    services["watch_target_service"].add(str(watched))
    manager = _build(services, tmp_path)

    assert manager.start() is True
    qtbot.wait(3000)
    manager.stop()
    qtbot.wait(1000)

    # 本テストが起動したclamonacc（watch-listのパスで識別）が残っていないこと
    remaining = [
        p for p in Path("/proc").glob("[0-9]*/cmdline")
        if str(manager.watch_list_path).encode() in _safe_read(p)
    ]
    assert remaining == []


def _safe_read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


@pytest.mark.skipif(
    not (
        _IS_LINUX and not _IS_ROOT and _HAS_CLAMONACC and shutil.which("pkexec")
        and os.environ.get("CLAMQT_TEST_PKEXEC_NO_AGENT") == "1"
    ),
    reason="一般ユーザー・認証エージェント無しの環境で CLAMQT_TEST_PKEXEC_NO_AGENT=1 の場合のみ実行",
)
def test_pkexec_without_auth_agent_reports_e2005(services, tmp_path, qtbot):
    watched = tmp_path / "watched"
    watched.mkdir()
    services["watch_target_service"].add(str(watched))
    manager = _build(services, tmp_path)

    with qtbot.waitSignal(manager.scan_error, timeout=30000) as blocker:
        manager.start()

    assert blocker.args[0].code == "E2005"
    assert manager.is_running is False
    manager.stop()
