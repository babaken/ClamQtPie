"""
ClamonaccRealtimeManager（Linuxリアルタイム監視: clamonacc連携）のテスト

clamonacc/pkexecはLinux専用でroot権限も必要なため、プロセスはFakeProcessで差し替える。
FakeProcessはrootシェル（_SUPERVISOR_SCRIPT）の振る舞い（標準入力を閉じると
clamonaccを止めて終了マーカーを出力する）を模擬する。
"""
import queue
import shutil
import subprocess

import pytest

from core.clamonacc_manager import (
    _FOUND_RE,
    _SUPERVISOR_SCRIPT,
    ClamonaccRealtimeManager,
    is_clamonacc_available,
)
from core.config_model import RealtimeConfig
from core.exclude_service import ExcludeService
from core.quarantine_service import QuarantineService
from core.realtime_factory import create_realtime_manager
from core.realtime_manager import RealtimeManager
from core.watch_target_service import WatchTargetService
from db.database import DatabaseService
from tests.conftest import FAKE_INFECTED_CONTENT


#: clamonaccの前提を満たすclamd.conf（テストが実機の/etc/clamav/clamd.confに左右されないよう明示する）
_GOOD_CLAMD_CONF = "LocalSocket /run/clamav/clamd.ctl\nUser clamav\nOnAccessExcludeUname clamav\n"


def _which(name):
    return {"clamonacc": "/usr/sbin/clamonacc", "pkexec": "/usr/bin/pkexec"}.get(name)


class _FakeStdin:
    def __init__(self, process):
        self._process = process
        self.closed = False

    def close(self):
        if not self.closed:
            self.closed = True
            self._process.finish(143)  # rootシェルがclamonaccをkillして終了マーカーを出す


class _FakeStdout:
    def __init__(self):
        self.lines: queue.Queue = queue.Queue()

    def __iter__(self):
        while True:
            line = self.lines.get()
            if line is None:
                return
            yield line


class FakeProcess:
    def __init__(self, command, **kwargs):
        self.command = command
        self.kwargs = kwargs
        self.returncode = None
        self.stdin = _FakeStdin(self)
        self.stdout = _FakeStdout()

    def finish(self, clamonacc_exit_code, marker=True):
        if marker:
            self.stdout.lines.put(f"__CLAMONACC_EXIT__ {clamonacc_exit_code}\n")
        self.stdout.lines.put(None)
        self.returncode = 0 if marker else clamonacc_exit_code

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture
def env(tmp_path):
    db_service = DatabaseService(tmp_path / "app.db")
    db_service.connect()
    watch_target_service = WatchTargetService(db_service)
    exclude_service = ExcludeService(db_service)
    quarantine_service = QuarantineService(db_service, tmp_path / "quarantine")
    watched = tmp_path / "watched"
    watched.mkdir()

    processes = []

    def popen(command, **kwargs):
        process = FakeProcess(command, **kwargs)
        processes.append(process)
        return process

    def build(clamd_conf=_GOOD_CLAMD_CONF, **config_overrides):
        """clamd_conf: clamd.confの内容（Noneならclamd.confが無い環境）"""
        candidates = ()
        if clamd_conf is not None:
            conf_path = tmp_path / "clamd.conf"
            conf_path.write_text(clamd_conf, encoding="utf-8")
            candidates = (str(conf_path),)
        config = RealtimeConfig(**config_overrides)
        return ClamonaccRealtimeManager(
            watch_target_service,
            exclude_service,
            quarantine_service,
            config,
            work_dir=tmp_path / "clamonacc",
            extra_exclude_dirs=[tmp_path / "logs"],
            popen=popen,
            which=_which,
            log_poll_interval_sec=0.05,
            clamd_conf_candidates=candidates,
        )

    yield {
        "build": build,
        "processes": processes,
        "watched": watched,
        "watch_target_service": watch_target_service,
        "exclude_service": exclude_service,
        "quarantine_service": quarantine_service,
        "tmp_path": tmp_path,
    }
    db_service.close()


def _append_log(manager, text):
    with open(manager.log_path, "a", encoding="utf-8") as f:
        f.write(text)


def test_start_without_targets_returns_false(env):
    manager = env["build"]()
    assert manager.start() is False
    assert env["processes"] == []


def test_start_writes_lists_and_builds_pkexec_command(env, monkeypatch):
    monkeypatch.setattr("core.clamonacc_manager._is_root", lambda: False)
    env["watch_target_service"].add(str(env["watched"]))
    env["exclude_service"].add("folder", "/home/user/ignored")
    env["exclude_service"].add("file", "/home/user/a.txt")  # フォルダ以外はexclude-listに含めない
    conf_path = env["tmp_path"] / "custom-clamd.conf"
    conf_path.write_text(_GOOD_CLAMD_CONF, encoding="utf-8")
    manager = env["build"](clamonacc_config_file=str(conf_path))

    assert manager.start() is True
    try:
        assert manager.watch_list_path.read_text(encoding="utf-8").splitlines() == [str(env["watched"])]
        assert manager.exclude_list_path.read_text(encoding="utf-8").splitlines() == [
            str(env["tmp_path"] / "logs"),
            "/home/user/ignored",
        ]
        command = env["processes"][0].command
        assert command[:3] == ["/usr/bin/pkexec", "/bin/sh", "-c"]
        clamonacc_args = command[command.index("/usr/sbin/clamonacc"):]
        assert "--foreground" in clamonacc_args
        assert f"--log={manager.log_path}" in clamonacc_args
        assert f"--watch-list={manager.watch_list_path}" in clamonacc_args
        assert f"--exclude-list={manager.exclude_list_path}" in clamonacc_args
        assert f"--config-file={conf_path}" in clamonacc_args
        assert "--fdpass" in clamonacc_args
        assert manager.is_running is True
    finally:
        manager.stop()


def test_root_runs_without_pkexec_and_fdpass_can_be_disabled(env, monkeypatch):
    monkeypatch.setattr("core.clamonacc_manager._is_root", lambda: True)
    env["watch_target_service"].add(str(env["watched"]))
    manager = env["build"](clamonacc_fdpass=False)

    manager.start()
    try:
        command = env["processes"][0].command
        assert command[0] == "/bin/sh"
        assert "--fdpass" not in command
        assert "--stream" in command  # fd渡しを使わない場合はファイルの内容を送る
    finally:
        manager.stop()


def test_found_line_in_log_triggers_detection_and_quarantine(env, qtbot):
    env["watch_target_service"].add(str(env["watched"]))
    infected = env["watched"] / "eicar.txt"
    infected.write_text(FAKE_INFECTED_CONTENT)
    manager = env["build"]()
    manager.start()
    try:
        with qtbot.waitSignal(manager.detected, timeout=3000) as blocker:
            _append_log(manager, f"Mon Oct 19 12:00:00 2026 -> {infected}: Eicar-Test-Signature FOUND\n")
        assert blocker.args == [str(infected), "Eicar-Test-Signature"]
        # 隔離はファイル移動→DB登録の順のため、ファイルが消えた時点ではまだDB未登録のことがある
        qtbot.waitUntil(lambda: len(env["quarantine_service"].list_all()) == 1, timeout=3000)
        assert not infected.exists()
    finally:
        manager.stop()


def test_log_lines_written_before_start_are_ignored(env, qtbot):
    env["watch_target_service"].add(str(env["watched"]))
    manager = env["build"]()
    manager.log_path.parent.mkdir(parents=True, exist_ok=True)
    _append_log(manager, "/old/file: Old-Signature FOUND\n")

    detected = []
    manager.detected.connect(lambda p, v: detected.append(p))
    manager.start()
    try:
        with qtbot.waitSignal(manager.detected, timeout=3000):
            _append_log(manager, "/new/file: New-Signature FOUND\n")
        assert detected == ["/new/file"]
    finally:
        manager.stop()


def test_partial_line_is_processed_after_completion(env, qtbot):
    env["watch_target_service"].add(str(env["watched"]))
    manager = env["build"](default_action="notify_only")
    manager.start()
    try:
        with qtbot.waitSignal(manager.detected, timeout=3000) as blocker:
            _append_log(manager, "/home/user/split")
            qtbot.wait(150)  # 書き込み途中の行は処理されないこと
            _append_log(manager, ".txt: Split-Signature FOUND\n")
        assert blocker.args == ["/home/user/split.txt", "Split-Signature"]
    finally:
        manager.stop()


def test_excluded_extension_skips_detection_action(env, qtbot):
    env["watch_target_service"].add(str(env["watched"]))
    env["exclude_service"].add("extension", ".log")
    manager = env["build"](default_action="notify_only")

    detected = []
    manager.detected.connect(lambda p, v: detected.append(p))
    manager.start()
    try:
        with qtbot.waitSignal(manager.detected, timeout=3000):
            _append_log(manager, "/home/user/app.log: X-Signature FOUND\n")
            _append_log(manager, "/home/user/app.bin: Y-Signature FOUND\n")
        assert detected == ["/home/user/app.bin"]
    finally:
        manager.stop()


def test_notify_only_does_not_quarantine(env, qtbot):
    env["watch_target_service"].add(str(env["watched"]))
    infected = env["watched"] / "eicar.txt"
    infected.write_text(FAKE_INFECTED_CONTENT)
    manager = env["build"](default_action="notify_only")
    manager.start()
    try:
        with qtbot.waitSignal(manager.detected, timeout=3000):
            _append_log(manager, f"{infected}: Eicar-Test-Signature FOUND\n")
        assert infected.exists()
        assert env["quarantine_service"].list_all() == []
    finally:
        manager.stop()


def test_stop_closes_stdin_without_error(env, qtbot):
    env["watch_target_service"].add(str(env["watched"]))
    manager = env["build"]()
    errors = []
    manager.scan_error.connect(errors.append)
    manager.start()
    process = env["processes"][0]

    manager.stop()
    qtbot.wait(100)

    assert process.stdin.closed is True
    assert manager.is_running is False
    assert errors == []


def test_unexpected_clamonacc_exit_emits_e2005(env, qtbot):
    env["watch_target_service"].add(str(env["watched"]))
    manager = env["build"]()
    manager.start()
    process = env["processes"][0]

    with qtbot.waitSignal(manager.scan_error, timeout=3000) as blocker:
        process.stdout.lines.put("ERROR: Clamonacc: could not connect to clamd\n")
        process.finish(2)

    error = blocker.args[0]
    assert error.code == "E2005"
    assert "終了コード 2" in error.detail
    assert "could not connect to clamd" in error.detail
    assert manager.is_running is False
    manager.stop()


def test_pkexec_auth_cancel_emits_e2005(env, qtbot):
    env["watch_target_service"].add(str(env["watched"]))
    manager = env["build"]()
    manager.start()
    process = env["processes"][0]

    with qtbot.waitSignal(manager.scan_error, timeout=3000) as blocker:
        process.finish(126, marker=False)

    assert "管理者認証" in blocker.args[0].detail
    manager.stop()


def test_missing_target_emits_watch_target_lost(env, qtbot):
    missing = str(env["tmp_path"] / "missing")
    env["watch_target_service"].add(missing)
    manager = env["build"]()

    with qtbot.waitSignal(manager.watch_target_lost, timeout=1000) as blocker:
        assert manager.start() is False

    assert blocker.args == [missing]
    assert env["watch_target_service"].list_enabled_paths() == []


def test_add_target_while_running_restarts_clamonacc(env):
    env["watch_target_service"].add(str(env["watched"]))
    manager = env["build"]()
    manager.start()
    other = env["tmp_path"] / "other"
    other.mkdir()
    env["watch_target_service"].add(str(other))

    manager.add_target(str(other))
    try:
        assert len(env["processes"]) == 2
        assert env["processes"][0].stdin.closed is True
        assert str(other) in manager.watch_list_path.read_text(encoding="utf-8")
    finally:
        manager.stop()


def test_found_regex_handles_prefix_and_colon_in_path():
    match = _FOUND_RE.search("Mon Oct 19 12:00:00 2026 -> /home/a: b/c.txt: Win.Test.EICAR_HDB-1 FOUND")
    assert match.group("path") == "/home/a: b/c.txt"
    assert match.group("virus") == "Win.Test.EICAR_HDB-1"
    assert _FOUND_RE.search("/home/a/ok.txt: OK") is None


# -- 方式の選択（realtime_factory） --------------------------------------------
def _factory(env, system, which, **config_overrides):
    return create_realtime_manager(
        clamav_service=None,
        watch_target_service=env["watch_target_service"],
        exclude_service=env["exclude_service"],
        quarantine_service=env["quarantine_service"],
        config=RealtimeConfig(**config_overrides),
        app_dir=env["tmp_path"],
        system=system,
        which=which,
    )


def test_factory_uses_watchdog_on_windows(env):
    assert isinstance(_factory(env, "Windows", _which), RealtimeManager)


def test_factory_uses_clamonacc_on_linux(env):
    manager = _factory(env, "Linux", _which)
    assert isinstance(manager, ClamonaccRealtimeManager)
    assert manager.mode_name == "clamonacc"


def test_factory_falls_back_to_watchdog_when_clamonacc_missing(env):
    manager = _factory(env, "Linux", lambda name: None)
    assert isinstance(manager, RealtimeManager)
    assert manager.mode_name == "watchdog監視"


def test_factory_respects_linux_backend_watchdog(env):
    assert isinstance(_factory(env, "Linux", _which, linux_backend="watchdog"), RealtimeManager)


def test_availability_requires_pkexec_when_not_root(monkeypatch):
    monkeypatch.setattr("core.clamonacc_manager._is_root", lambda: False)
    available, reason = is_clamonacc_available(
        RealtimeConfig(), lambda name: "/usr/sbin/clamonacc" if name == "clamonacc" else None
    )
    assert available is False
    assert "pkexec" in reason


# -- rootシェル（_SUPERVISOR_SCRIPT）の実動作 -----------------------------------
@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shが無い環境")
def test_supervisor_script_stops_child_when_stdin_closes():
    # clamonaccの代わりにsleepを監視させ、標準入力を閉じたら終了マーカーが出ることを確認
    process = subprocess.Popen(
        [shutil.which("sh"), "-c", _SUPERVISOR_SCRIPT, "supervisor", "sleep", "30"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    process.stdin.close()
    out, _ = process.communicate(timeout=10)
    assert out.startswith("__CLAMONACC_EXIT__ ")
    assert out.split()[1] != "0"  # killされている


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX shが無い環境")
def test_supervisor_script_reports_child_exit_code():
    process = subprocess.Popen(
        [shutil.which("sh"), "-c", _SUPERVISOR_SCRIPT, "supervisor", "sh", "-c", "exit 3"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        line = process.stdout.readline()
        assert line.strip() == "__CLAMONACC_EXIT__ 3"
    finally:
        process.stdin.close()  # 監視用サブシェルを終了させる
        process.wait(timeout=10)


def test_folder_exclude_change_restarts_but_extension_change_does_not(env):
    env["watch_target_service"].add(str(env["watched"]))
    manager = env["build"]()
    manager.start()
    try:
        manager.on_excludes_changed("extension")  # アプリ側で照合するため再起動不要
        assert len(env["processes"]) == 1

        env["exclude_service"].add("folder", "/home/user/new-excluded")
        manager.on_excludes_changed("folder")  # --exclude-listの再生成に再起動が必要
        assert len(env["processes"]) == 2
        assert "/home/user/new-excluded" in manager.exclude_list_path.read_text(encoding="utf-8")
    finally:
        manager.stop()


# -- clamonaccの仕様に合わせた確認（Linuxリアルタイム監視詳細設計書11章） ----------------
@pytest.mark.parametrize(
    "marker_line",
    [
        "ERROR: ClamInotif: can't exclude '/home/user/app/logs'",
        "ERROR: ClamInotif: could not watch path '/home/user', No space left on device",
        "ERROR: ClamInotif: Not watching path '/tmp'",
    ],
)
def test_monitoring_setup_failure_stops_clamonacc_and_emits_e2005(env, qtbot, marker_line):
    """clamonaccは監視の設定に失敗してもプロセスが終了しないため、ログ行で検出して停止する"""
    env["watch_target_service"].add(str(env["watched"]))
    manager = env["build"]()
    errors = []
    manager.scan_error.connect(errors.append)
    manager.start()
    process = env["processes"][0]

    with qtbot.waitSignal(manager.scan_error, timeout=3000) as blocker:
        _append_log(manager, "ClamInotif: watching '/home/user' (and all sub-directories)\n")
        _append_log(manager, marker_line + "\n")

    error = blocker.args[0]
    assert error.code == "E2005"
    assert "監視を設定できませんでした" in error.detail
    assert manager.is_running is False
    assert process.stdin.closed is True  # rootシェルにclamonaccを停止させる
    qtbot.wait(200)
    assert len(errors) == 1  # 停止に伴う終了マーカーを異常終了として二重に通知しない
    manager.stop()


def test_monitoring_setup_failure_hints(env, qtbot):
    env["watch_target_service"].add(str(env["watched"]))
    manager = env["build"]()
    manager.start()
    with qtbot.waitSignal(manager.scan_error, timeout=3000) as blocker:
        _append_log(manager, "ERROR: ClamInotif: can't exclude '/home/user/app/logs'\n")
    assert "Issue #1797" in blocker.args[0].detail
    assert "watchdog" in blocker.args[0].detail
    manager.stop()


def test_can_restart_after_monitoring_setup_failure(env, qtbot):
    env["watch_target_service"].add(str(env["watched"]))
    manager = env["build"]()
    manager.start()
    with qtbot.waitSignal(manager.scan_error, timeout=3000):
        _append_log(manager, "ERROR: ClamInotif: can't exclude '/x'\n")

    assert manager.start() is True
    try:
        assert len(env["processes"]) == 2
        assert manager.is_running is True
    finally:
        manager.stop()


def test_missing_exclude_user_in_clamd_conf_blocks_start_before_auth(env):
    """ローカルのclamdではOnAccessExcludeUname等が無いとclamonaccが起動を拒否する（推奨ではなく必須）"""
    env["watch_target_service"].add(str(env["watched"]))
    manager = env["build"](clamd_conf="LocalSocket /run/clamav/clamd.ctl\nUser clamscan\n")
    errors = []
    manager.scan_error.connect(errors.append)

    assert manager.start() is False
    assert env["processes"] == []  # 認証ダイアログを出す前に止める
    assert errors[0].code == "E2005"
    assert "OnAccessExcludeUname clamscan" in errors[0].detail  # clamdの実行ユーザー名で案内する


@pytest.mark.parametrize(
    "conf_line",
    ["OnAccessExcludeUID 113", "OnAccessExcludeRootUID yes", "OnAccessExcludeUname clamav"],
)
def test_any_exclude_user_option_satisfies_clamonacc(env, conf_line):
    env["watch_target_service"].add(str(env["watched"]))
    manager = env["build"](clamd_conf=f"LocalSocket /run/clamav/clamd.ctl\n{conf_line}\n")
    assert manager.start() is True
    manager.stop()


def test_remote_clamd_does_not_require_exclude_user(env):
    env["watch_target_service"].add(str(env["watched"]))
    manager = env["build"](clamd_conf="TCPSocket 3310\nTCPAddr 192.168.1.10\n")
    assert manager.start() is True
    try:
        command = env["processes"][0].command
        assert "--stream" in command  # LocalSocketが無いとfd渡しは使えない
        assert "--fdpass" not in command
    finally:
        manager.stop()


def test_exit_with_exclude_user_error_shows_hint(env, qtbot):
    env["watch_target_service"].add(str(env["watched"]))
    manager = env["build"](clamd_conf=None)  # clamd.confが読めない場合はclamonaccの判定に任せる
    manager.start()
    process = env["processes"][0]
    with qtbot.waitSignal(manager.scan_error, timeout=3000) as blocker:
        process.stdout.lines.put(
            "ERROR: Clamonacc: at least one of OnAccessExcludeUID, OnAccessExcludeUname, "
            "or OnAccessExcludeRootUID must be specified ...\n"
        )
        process.finish(2)
    assert "OnAccessExcludeUname clamav" in blocker.args[0].detail
    manager.stop()


def test_disable_ddd_blocks_start(env):
    env["watch_target_service"].add(str(env["watched"]))
    manager = env["build"](clamd_conf=_GOOD_CLAMD_CONF + "OnAccessDisableDDD yes\n")
    errors = []
    manager.scan_error.connect(errors.append)

    assert manager.start() is False
    assert "OnAccessDisableDDD" in errors[0].detail


def test_mount_path_is_warned_but_started(env, caplog):
    env["watch_target_service"].add(str(env["watched"]))
    manager = env["build"](clamd_conf=_GOOD_CLAMD_CONF + "OnAccessMountPath /home\nOnAccessDisableDDD yes\n")
    assert manager.start() is True
    try:
        assert "OnAccessMountPath" in caplog.text
    finally:
        manager.stop()


def test_clamd_temporary_directory_and_root_are_not_watched(env, qtbot):
    """clamdの一時フォルダを--watch-listに書くとclamonaccが無限ループし、/は無視されるため外す"""
    clamd_tmp = env["tmp_path"] / "clamdtmp"
    clamd_tmp.mkdir()
    env["watch_target_service"].add(str(env["watched"]))
    env["watch_target_service"].add(str(clamd_tmp) + "/")
    env["watch_target_service"].add("/")
    manager = env["build"](clamd_conf=_GOOD_CLAMD_CONF + f"TemporaryDirectory {clamd_tmp}\n")
    errors = []
    manager.scan_error.connect(errors.append)

    assert manager.start() is True
    try:
        assert manager.watch_list_path.read_text(encoding="utf-8").splitlines() == [str(env["watched"])]
        assert errors[0].code == "E2006"
        assert manager.is_running is True  # 外したフォルダ以外は監視を続ける
        # 監視対象の設定自体は残す（watchdog方式に切り替えれば監視できるため）
        assert len(env["watch_target_service"].list_enabled_paths()) == 3
    finally:
        manager.stop()


def test_only_unwatchable_targets_does_not_start(env):
    env["watch_target_service"].add("/")
    manager = env["build"]()
    errors = []
    manager.scan_error.connect(errors.append)

    assert manager.start() is False
    assert env["processes"] == []
    assert errors[0].code == "E2006"


def test_default_temporary_directory_is_tmp(env):
    env["watch_target_service"].add("/tmp")
    manager = env["build"]()  # TemporaryDirectory未指定
    errors = []
    manager.scan_error.connect(errors.append)
    assert manager.start() is False
    assert errors[0].code == "E2006"


def test_duplicate_found_for_same_file_is_handled_once(env, qtbot):
    """隔離時にアプリがファイルを開くと、clamonaccが同じファイルを再検査してFOUNDを再度出力する"""
    env["watch_target_service"].add(str(env["watched"]))
    infected = env["watched"] / "eicar.txt"
    infected.write_text(FAKE_INFECTED_CONTENT)
    manager = env["build"]()
    detected = []
    manager.detected.connect(lambda p, v: detected.append(p))
    manager.start()
    try:
        with qtbot.waitSignal(manager.detected, timeout=3000):
            _append_log(manager, f"{infected}: Eicar-Test-Signature FOUND\n")
        qtbot.waitUntil(lambda: len(env["quarantine_service"].list_all()) == 1, timeout=3000)
        _append_log(manager, f"{infected}: Eicar-Test-Signature FOUND\n")
        with qtbot.waitSignal(manager.detected, timeout=3000):
            _append_log(manager, "/home/user/other.bin: Other-Signature FOUND\n")
        assert detected == [str(infected), "/home/user/other.bin"]
        assert len(env["quarantine_service"].list_all()) == 1
    finally:
        manager.stop()


def test_same_file_is_detected_again_after_window(env, qtbot, monkeypatch):
    env["watch_target_service"].add(str(env["watched"]))
    manager = env["build"](default_action="notify_only")
    clock = {"now": 1000.0}
    monkeypatch.setattr("core.clamonacc_manager.time.monotonic", lambda: clock["now"])
    detected = []
    manager.detected.connect(lambda p, v: detected.append(p))

    manager._handle_detection("/home/user/a.bin", "X")
    clock["now"] += 5
    manager._handle_detection("/home/user/a.bin", "X")
    clock["now"] += 10
    manager._handle_detection("/home/user/a.bin", "X")

    assert detected == ["/home/user/a.bin", "/home/user/a.bin"]


def test_factory_passes_app_dirs_to_watchdog_manager(env):
    # watchdog方式でもアプリ自身のフォルダを除外する（隔離の無限ループ対策）
    manager = _factory(env, "Windows", _which)
    app_dir = env["tmp_path"]
    assert manager._extra_exclude_dirs == [app_dir / "logs", app_dir / "quarantine", app_dir / "clamonacc"]


def test_detection_handled_reports_actual_quarantine_result(env, qtbot):
    # レビュー指摘: 隔離に失敗しても通知に「自動的に隔離されました」と出ていた。
    # 通知（app.pyでnotify_detectionに接続）は、隔離を試した後の実際の結果で行う
    env["watch_target_service"].add(str(env["watched"]))
    manager = env["build"](default_action="quarantine")
    handled = []
    manager.detection_handled.connect(lambda p, v, q: handled.append((p, q)))

    infected = env["watched"] / "infected.bin"
    infected.write_bytes(b"x")
    manager._handle_detection(str(infected), "X")
    missing = env["watched"] / "missing.bin"  # 隔離できない（ファイルが無い）
    manager._handle_detection(str(missing), "X")

    assert handled == [(str(infected), True), (str(missing), False)]


def test_detection_handled_is_false_for_notify_only(env, qtbot):
    env["watch_target_service"].add(str(env["watched"]))
    manager = env["build"](default_action="notify_only")
    handled = []
    manager.detection_handled.connect(lambda p, v, q: handled.append(q))

    manager._handle_detection("/home/user/a.bin", "X")
    assert handled == [False]


def test_detection_inside_app_dir_is_ignored(env, qtbot):
    env["watch_target_service"].add(str(env["watched"]))
    manager = env["build"](default_action="notify_only")
    detected = []
    manager.detected.connect(lambda p, v: detected.append(p))

    manager._handle_detection(str(env["tmp_path"] / "logs" / "x.bin"), "X")
    manager._handle_detection("/home/user/a.bin", "X")

    assert detected == ["/home/user/a.bin"]
