"""
システム診断（core/diagnostics_service.py、画面レイアウト刷新で追加）のテスト
"""
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

from core.config_model import ConfigModel
from core.exec_path import system_tool
from core.diagnostics_service import (
    LEVEL_NG,
    LEVEL_OK,
    LEVEL_WARN,
    REPORT_TITLE,
    DiagnosticsService,
    is_sensitive_host,
    mask_report,
)


def _config_service(config=None):
    return MagicMock(current=config or ConfigModel())


def _runner(stdout=""):
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout, "")

    return run


def _clamav(ping=True):
    service = MagicMock()
    service.endpoint = "TCP 127.0.0.1:3310"
    service.ping.return_value = ping
    service.get_version.return_value = ("ClamAV 1.5.4", "28157", "Sun Oct 18 2026")
    return service


def _install(tmp_path):
    """Windowsの既定インストール先（Program Files\\ClamAV）を模したフォルダ"""
    clamav_dir = tmp_path / "ClamAV"
    (clamav_dir / "database").mkdir(parents=True)
    for name in ("clamd", "freshclam", "clamscan"):
        (clamav_dir / f"{name}.exe").write_bytes(b"")
    (clamav_dir / "clamd.conf").write_text("TCPSocket 3310\n", encoding="utf-8")
    (clamav_dir / "database" / "main.cvd").write_bytes(b"")
    return clamav_dir


def test_report_finds_windows_install_and_clamd(tmp_path):
    clamav_dir = _install(tmp_path)
    service = DiagnosticsService(
        _config_service(), _clamav(), MagicMock(is_running=True, mode_name="watchdog監視"),
        system="Windows", which=lambda name: None,
        runner=_runner("STATE              : 4  RUNNING"), environ={"ProgramFiles": str(tmp_path)},
    )
    report = service.run()
    text = report.to_text()

    assert text.startswith(f"=== {REPORT_TITLE} ===")
    assert REPORT_TITLE == "ClamQtPie 診断レポート"
    assert str(clamav_dir / "clamd.exe") in text
    assert str(clamav_dir / "clamd.conf") in text
    assert "main.cvd" in text
    assert "clamd: RUNNING" in text
    assert "ClamAV 1.5.4 / 28157" in text
    assert "（Windowsでは使用しません）" in text
    assert report.has_problem is False
    assert any(c.level == LEVEL_OK and "応答しました" in c.message for c in report.checks)
    # レビュー指摘: 定義更新はPATHのfreshclamしか起動しないため、既定のインストール先で見つけてもOKにしない
    freshclam = [c for c in report.checks if c.message.startswith("freshclam")]
    assert [c.level for c in freshclam] == [LEVEL_WARN]
    assert str(clamav_dir / "freshclam.exe") in freshclam[0].message
    assert "PATH に追加" in freshclam[0].message
    assert "PATH にありません" in text


def test_report_flags_missing_engine_and_unreachable_clamd():
    service = DiagnosticsService(
        _config_service(), _clamav(ping=False), system="Windows", which=lambda name: None,
        runner=_runner("[SC] EnumQueryServicesStatus:OpenService FAILED 1060:"), environ={},
    )
    report = service.run()
    messages = [c.message for c in report.checks if c.level == LEVEL_NG]
    warnings = [c.message for c in report.checks if c.level == LEVEL_WARN]

    assert report.has_problem is True
    # clamdはOSのサービスとして起動するため、PATHに無いことは判定に使わない（PING・サービスの状態で判定）
    assert not any(c.message.startswith("clamd が") for c in report.checks)
    assert any("freshclam が PATH に見つかりません" in m for m in warnings)
    assert any("接続できません" in m for m in messages)
    assert "clamd: 登録されていません" in report.to_text()


@pytest.mark.skipif(sys.platform != "win32", reason="OEMコードページはWindowsのみ")
def test_service_state_reads_japanese_sc_output():
    # Python 3.15（UTF-8モード既定）でも、日本語Windowsのscの出力（cp932）を読めること
    message = "[SC] EnumQueryServicesStatus:OpenService FAILED 1060:\r\n\r\n指定されたサービスはインストールされていません。\r\n"
    script = f"import sys; sys.stdout.buffer.write({message.encode('cp932')!r}); sys.exit(1060)"

    def run(command, **kwargs):
        return subprocess.run([sys.executable, "-c", script], **kwargs)

    service = DiagnosticsService(
        _config_service(), _clamav(), system="Windows", which=lambda name: None, runner=run, environ={}
    )
    assert service._service_state() == "clamd: 登録されていません"


def test_mask_report_hides_home_user_and_host():
    text = "C:\\Users\\taro-y\\Desktop\\a.txt C:/Users/taro-y/x user=taro-y host=PC-0123 kk"
    masked = mask_report(text, home="C:\\Users\\taro-y", user="taro-y", host="PC-0123")

    assert "taro-y" not in masked
    assert "PC-0123" not in masked
    assert "~\\Desktop\\a.txt" in masked
    assert "~/x" in masked
    assert "<ユーザー名>" in masked and "<コンピューター名>" in masked


def test_mask_report_keeps_short_user_name():
    assert mask_report("kk ok", home="/home/k", user="k", host="") == "kk ok"


def test_missing_configured_clamd_conf_is_reported_and_defaults_are_searched(tmp_path):
    # レビュー指摘: 存在しない指定パスをclamd.confの場所として表示していた
    default_conf = tmp_path / "clamd.conf"
    default_conf.write_text(f"DatabaseDirectory {tmp_path}\n", encoding="utf-8")
    (tmp_path / "daily.cld").write_bytes(b"")
    config = ConfigModel()
    config.realtime.clamonacc_config_file = str(tmp_path / "missing.conf")
    service = DiagnosticsService(
        _config_service(config), _clamav(), system="Linux", which=lambda name: f"/usr/bin/{name}",
        runner=_runner("active"), environ={},
    )
    monkey_candidates = (str(default_conf),)
    service._conf_candidates = lambda conf_name, clamd_path: monkey_candidates  # 既定の場所を差し替える

    report = service.run()
    text = report.to_text()

    assert any("指定された clamd.conf が見つかりません" in c.message for c in report.checks)
    assert str(default_conf) in text
    assert "missing.conf" not in text.split("--- ヘルスチェック結果 ---")[0]
    assert "daily.cld" in text


def test_freshclam_found_at_configured_path():
    path = "C:\\Tools\\ClamAV\\freshclam.exe"
    config = ConfigModel()
    config.update.freshclam_path = path
    service = DiagnosticsService(
        _config_service(config), _clamav(), system="Windows",
        which=lambda name: name if name == path else None, runner=_runner(), environ={},
    )

    report = service.run()
    assert any(c.level == LEVEL_OK and c.message == f"freshclam を検出しました: {path}" for c in report.checks)


def test_missing_configured_freshclam_names_the_setting():
    config = ConfigModel()
    config.update.freshclam_path = "/opt/clamav/bin/freshclam"
    service = DiagnosticsService(
        _config_service(config), _clamav(), system="Linux", which=lambda name: None,
        runner=_runner("active"), environ={},
    )

    report = service.run()
    assert any(
        c.level == LEVEL_WARN and "設定で指定した場所に見つかりません（/opt/clamav/bin/freshclam）" in c.message
        for c in report.checks
    )


def test_linux_sbin_location_is_only_a_hint(tmp_path, monkeypatch):
    # レビュー指摘: 一般ユーザーのPATHに/usr/sbinが無いディストリビューション（Debian等）
    (tmp_path / "clamonacc").write_bytes(b"")
    (tmp_path / "clamd").write_bytes(b"")
    monkeypatch.setattr("core.diagnostics_service._LINUX_SBIN_DIRS", (str(tmp_path),))
    service = DiagnosticsService(
        _config_service(), _clamav(), system="Linux",
        which=lambda name: "/usr/bin/freshclam" if name == "freshclam" else None,
        runner=_runner("active"), environ={},
    )

    report = service.run()
    clamonacc = [c for c in report.checks if c.message.startswith("clamonacc")]
    assert [c.level for c in clamonacc] == [LEVEL_WARN]
    assert str(tmp_path / "clamonacc") in clamonacc[0].message
    assert "「ClamAV エンジン」タブ" in clamonacc[0].message
    assert f"PATH にありません（{tmp_path / 'clamd'} にあります）" in report.to_text()
    assert not any(c.message.startswith("clamd が") for c in report.checks)


def test_clamonacc_not_checked_when_watchdog_backend_selected():
    config = ConfigModel()
    config.realtime.linux_backend = "watchdog"
    service = DiagnosticsService(
        _config_service(config), _clamav(), system="Linux", which=lambda name: None,
        runner=_runner("active"), environ={},
    )

    report = service.run()
    assert not any(c.message.startswith("clamonacc") for c in report.checks)


def _service_with_conf(tmp_path, conf_text, config=None, system="Linux"):
    conf = tmp_path / "clamd.conf"
    conf.write_text(conf_text, encoding="utf-8")
    service = DiagnosticsService(
        _config_service(config), _clamav(), system=system, which=lambda name: f"/usr/bin/{name}",
        runner=_runner("active"), environ={},
    )
    service._conf_candidates = lambda conf_name, clamd_path: (str(conf),) if conf_name == "clamd.conf" else ()
    return service


def _listen_warnings(report):
    """clamd.confの待ち受け先とアプリの接続先の食い違いの注意"""
    return [
        c.message for c in report.checks
        if c.level == LEVEL_WARN and ("clamd.conf の" in c.message or "clamd.conf に" in c.message)
    ]


def test_listen_address_mismatch_tcp(tmp_path):
    config = ConfigModel()
    config.clamd.connection_type = "tcp"
    service = _service_with_conf(tmp_path, "TCPSocket 3311\nTCPAddr 192.168.1.10\n", config)

    warnings = _listen_warnings(service.run())
    assert any("TCPSocket（3311）" in m and "（3310）" in m for m in warnings)
    assert all("「ClamAV エンジン」タブ" in m for m in warnings)
    assert any("TCPAddr（192.168.1.10）" in m and "127.0.0.1" in m for m in warnings)


def test_listen_address_matches_tcp_loopback_and_any(tmp_path):
    config = ConfigModel()
    config.clamd.connection_type = "tcp"
    for conf_text in (
        "TCPSocket 3310\nTCPAddr localhost\n",
        "TCPSocket 3310\nTCPAddr 0.0.0.0\n",
        "TCPSocket 3310\n",
    ):
        service = _service_with_conf(tmp_path, conf_text, config)
        assert _listen_warnings(service.run()) == [], conf_text


def test_listen_address_missing_tcp_socket(tmp_path):
    service = _service_with_conf(tmp_path, "LocalSocket /run/clamav/clamd.ctl\n", system="Windows")

    warnings = _listen_warnings(service.run())
    assert any("TCPSocket がありません" in m for m in warnings)


def test_listen_address_mismatch_unix_socket(tmp_path):
    service = _service_with_conf(tmp_path, "LocalSocket /tmp/other-clamd.sock\n")

    warnings = _listen_warnings(service.run())
    assert any("LocalSocket（/tmp/other-clamd.sock）" in m and "/var/run/clamav/clamd.ctl" in m for m in warnings)


def test_listen_address_matches_unix_socket(tmp_path):
    service = _service_with_conf(tmp_path, "LocalSocket /var/run/clamav/clamd.ctl\n")
    assert _listen_warnings(service.run()) == []


def test_is_sensitive_host():
    # ループバック・プライベートIPは伏せず、ホスト名とグローバルIPは伏せる
    assert not is_sensitive_host("127.0.0.1")
    assert not is_sensitive_host("localhost")
    assert not is_sensitive_host("::1")
    assert not is_sensitive_host("192.168.1.10")
    assert not is_sensitive_host("10.0.0.5")
    assert not is_sensitive_host("172.16.0.1")
    assert is_sensitive_host("clamav-srv01")
    assert is_sensitive_host("8.8.8.8")


def test_masked_report_hides_connection_and_paths(tmp_path):
    # レビュー指摘: 伏せ字版でもclamdの接続先・ソケット・設定ファイルのパス等がそのまま残っていた
    conf = tmp_path / "clamd.conf"
    conf.write_text("TCPSocket 3310\nTCPAddr clamav-srv01\n", encoding="utf-8")
    config = ConfigModel()
    config.clamd.connection_type = "tcp"
    config.clamd.tcp_host = "clamav-srv01"
    config.realtime.clamonacc_path = "/opt/clamav/sbin/clamonacc"
    app_dir = tmp_path / "ClamQt"
    clamav = _clamav(ping=False)
    clamav.endpoint = "TCP clamav-srv01:3310"
    service = DiagnosticsService(
        _config_service(config), clamav, app_dir=app_dir, system="Linux",
        which=lambda name: name if name.startswith("/") else f"/usr/bin/{name}",
        runner=_runner("active"), environ={},
    )
    service._conf_candidates = lambda conf_name, clamd_path: (str(conf),) if conf_name == "clamd.conf" else ()

    report = service.run()
    text = report.to_text()
    masked = mask_report(text, home=str(tmp_path), user="", host="", extra=report.sensitive)

    for secret in ("clamav-srv01", str(conf), "/opt/clamav/sbin/clamonacc", str(app_dir)):
        assert secret in text, secret
        assert secret not in masked, secret
    assert report.sensitive["/var/run/clamav/clamd.ctl"] == "<ソケット>"
    assert "<clamdのホスト>" in masked
    assert "<アプリのフォルダ>" in masked  # ホーム配下でも「~」でなく丸ごと伏せる
    assert "<clamd.conf>" in masked


def test_masked_report_keeps_private_ip():
    config = ConfigModel()
    config.clamd.tcp_host = "192.168.1.10"
    service = DiagnosticsService(
        _config_service(config), _clamav(), system="Windows", which=lambda name: None,
        runner=_runner(), environ={},
    )
    report = service.run()
    assert "192.168.1.10" not in report.sensitive
    masked = mask_report("TCP 192.168.1.10:3310", home="/x", user="", host="", extra=report.sensitive)
    assert masked == "TCP 192.168.1.10:3310"


def test_service_state_uses_os_default_service_name():
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "active", "")

    service = DiagnosticsService(
        _config_service(), _clamav(), system="Linux", which=lambda name: None, runner=run, environ={}
    )
    service.run()
    assert [system_tool("systemctl", "Linux"), "is-active", "clamav-daemon"] in calls


def test_reads_latest_config_on_each_run():
    # レビュー指摘: 設定画面で保存した内容（ConfigService.currentの差し替え）を反映していなかった
    config_service = _config_service()
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "STATE : 4  RUNNING", "")

    service = DiagnosticsService(
        config_service, _clamav(), system="Windows", which=lambda name: None, runner=run, environ={}
    )
    new_config = ConfigModel()
    new_config.clamd.service_name = "ClamAV-Custom"
    config_service.current = new_config

    service.run()
    assert [system_tool("sc", "Windows"), "query", "ClamAV-Custom"] in calls


def _sc_runner(binary_path_name):
    """sc query / sc qc を模す（qcはBINARY_PATH_NAMEを返す）"""

    def run(command, **kwargs):
        if command[:2] == [system_tool("sc", "Windows"), "qc"]:
            return subprocess.CompletedProcess(command, 0, f"        BINARY_PATH_NAME   : {binary_path_name}\r\n", "")
        return subprocess.CompletedProcess(command, 0, "STATE : 4  RUNNING", "")

    return run


def test_windows_uses_service_binary_and_config_file(tmp_path):
    # Chocolatey等: PATH上のclamd.exeは中継ファイル（shim）で、隣にclamd.confや定義DBが無い。
    # サービスの登録内容（sc qc）から本体と--config-fileを知る（Windows実機の検証で発見）
    shim_dir = tmp_path / "chocolatey" / "bin"
    shim_dir.mkdir(parents=True)
    (shim_dir / "clamd.exe").write_bytes(b"")
    real_dir = tmp_path / "lib" / "clamav-1.5.4.win.x64"
    real_dir.mkdir(parents=True)
    (real_dir / "clamd.exe").write_bytes(b"")
    conf_dir = tmp_path / "clamav-webui"
    (conf_dir / "database").mkdir(parents=True)
    (conf_dir / "database" / "daily.cld").write_bytes(b"")
    (conf_dir / "freshclam.conf").write_text("", encoding="utf-8")
    clamd_conf = conf_dir / "clamd.conf"
    clamd_conf.write_text(f'TCPSocket 3310\nDatabaseDirectory "{conf_dir / "database"}"\n', encoding="utf-8")

    service = DiagnosticsService(
        _config_service(), _clamav(), system="Windows",
        which=lambda name: str(shim_dir / f"{name}.exe") if name == "clamd" else None,
        runner=_sc_runner(f'"{real_dir / "clamd.exe"}" --config-file="{clamd_conf}" --daemon --service-mode'),
        environ={},
    )
    report = service.run()
    text = report.to_text()

    assert str(real_dir / "clamd.exe") in text
    assert f"clamd.conf を読み込みました: {clamd_conf}" in text
    assert str(conf_dir / "freshclam.conf") in text
    assert "daily.cld" in text
    assert not any("定義ファイル" in c.message and c.level == LEVEL_NG for c in report.checks)


def test_missing_definitions_is_warning_while_clamd_responds():
    # 定義ファイルの場所を特定できなくても、clamdが定義を読み込んで応答していればNGにしない
    service = DiagnosticsService(
        _config_service(), _clamav(ping=True), system="Windows", which=lambda name: None,
        runner=_sc_runner(""), environ={},
    )
    report = service.run()
    assert any(c.level == LEVEL_WARN and "定義ファイルの場所を特定できません" in c.message for c in report.checks)
    assert not any(c.level == LEVEL_NG and "定義ファイル" in c.message for c in report.checks)


def test_missing_definitions_is_ng_when_clamd_does_not_respond():
    service = DiagnosticsService(
        _config_service(), _clamav(ping=False), system="Windows", which=lambda name: None,
        runner=_sc_runner(""), environ={},
    )
    report = service.run()
    assert any(c.level == LEVEL_NG and "定義ファイルが見つかりません" in c.message for c in report.checks)


@pytest.mark.parametrize("conf_line, warned", [("", True), ("AlertExceedsMax no\n", True), ("AlertExceedsMax yes\n", False)])
def test_alert_exceeds_max_is_checked(tmp_path, conf_line, warned):
    # 上限を超えたファイルが検査されないまま「問題なし」にならないよう、AlertExceedsMaxを確かめる
    conf = tmp_path / "clamd.conf"
    conf.write_text(f"DatabaseDirectory {tmp_path}\n{conf_line}", encoding="utf-8")
    service = DiagnosticsService(
        _config_service(), _clamav(), system="Linux", which=lambda name: f"/usr/bin/{name}",
        runner=_runner("active"), environ={},
    )
    service._conf_candidates = lambda conf_name, clamd_path: (str(conf),)

    report = service.run()

    assert any("AlertExceedsMax" in c.message for c in report.checks) is warned
