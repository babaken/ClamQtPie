"""
Windows のインストーラから呼ぶ導入・削除の処理（サービス化・Web API設計書 11章、12章 32。段階 12-4d）

Inno Setup のスクリプトには複雑な処理を書かず、clamqtpie-ctl setup install|uninstall を呼ぶ（テストできるように）。
- データの置き場所（%ProgramData%\\ClamQtPie）を一般の利用者が作っていた・書き換えられるなら、
  ClamQtPie.untrusted-<日時> に退避して作り直す（C:\\ProgramData の直下は一般の利用者もフォルダを作れるため）
- ClamQtPie Users グループを作り、導入した利用者を入れる
- サービスを登録（遅延自動起動）・回復動作（60 秒後に起動し直す、2 回まで、1 日で数え直す）・開始する
- 削除: サービスを止めて登録を消す。--purge ならデータとグループも消す
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from core.exec_path import child_env, no_window_kwargs, system_tool

SERVICE_NAME = "ClamQtPie"
SERVICE_EXE = "ClamQtPieService.exe"
#: サービスが止まるのを待つ時間
_STOP_WAIT_SEC = 60


class SetupError(RuntimeError):
    pass


def _run(cmd: list[str], runner=subprocess.run) -> subprocess.CompletedProcess:
    from core.clamd_process import console_text_kwargs

    try:
        return runner(cmd, capture_output=True, timeout=120, env=child_env(None, "Windows"),
                      **console_text_kwargs("Windows"), **no_window_kwargs("Windows"))
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetupError(f"{Path(cmd[0]).name} を実行できません ({exc})") from exc


def _sc(*args: str, runner=subprocess.run) -> subprocess.CompletedProcess:
    return _run([system_tool("sc", "Windows"), *args], runner)


def service_command(app_dir: Path) -> list[str]:
    """サービスの実行ファイル（配布版）。開発時は service_main.py"""
    if getattr(sys, "frozen", False):
        return [str(app_dir / SERVICE_EXE)]
    return [sys.executable, str(Path(__file__).resolve().parent.parent / "service_main.py")]


# -- データの置き場所 ----------------------------------------------------------------------------
def set_aside_untrusted(data_dir: Path, now: datetime | None = None, check=None) -> Path | None:
    """一般の利用者が作った・書き換えられるデータの置き場所を退避する。退避した先（問題が無ければ None）"""
    if check is None:
        from core.service_paths import writable_by_others as check
    if not data_dir.exists() and not data_dir.is_symlink():
        return None
    problem = "リンク・ジャンクションになっています" if data_dir.is_symlink() else check(data_dir)
    if not problem:
        return None
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    target = data_dir.with_name(f"{data_dir.name}.untrusted-{stamp}")
    data_dir.rename(target)
    return target


# -- サービス ------------------------------------------------------------------------------------
def service_installed(runner=subprocess.run) -> bool:
    return _sc("query", SERVICE_NAME, runner=runner).returncode == 0


def register_service(app_dir: Path, runner=subprocess.run) -> None:
    command = "update" if service_installed(runner) else "install"
    # 通常の自動起動（遅延にすると、サインインした直後のトレイGUI が約 2 分つながらない。更新でも遅延を外す）
    result = _run([*service_command(app_dir), "--startup", "auto", command], runner)
    if result.returncode != 0 or not service_installed(runner):
        raise SetupError(f"サービスを登録できません: {(result.stderr or result.stdout or '').strip()[:300]}")
    # 失敗したら 60 秒後に起動し直す（2 回まで、1 日で数え直す）。エラーで止まったときも対象にする（12-3d-2）
    _sc("failure", SERVICE_NAME, "reset=", "86400", "actions=", "restart/60000/restart/60000", runner=runner)
    _sc("failureflag", SERVICE_NAME, "1", runner=runner)


def start_service(runner=subprocess.run) -> None:
    result = _sc("start", SERVICE_NAME, runner=runner)
    # 1056: 既に動いている
    if result.returncode not in (0, 1056):
        raise SetupError(f"サービスを開始できません（{result.returncode}）: {(result.stdout or '').strip()[:300]}")


def stop_service(runner=subprocess.run, sleep=time.sleep, wait_sec: int = _STOP_WAIT_SEC) -> None:
    """止めて、止まるまで待つ（ファイルを差し替える・消す前に）"""
    if not service_installed(runner):
        return
    _sc("stop", SERVICE_NAME, runner=runner)
    for _ in range(wait_sec):
        state = _sc("query", SERVICE_NAME, runner=runner).stdout or ""
        if "STOPPED" in state or "RUNNING" not in state and "PENDING" not in state:
            wait_process_exit(runner, sleep)
            return
        sleep(1)
    raise SetupError("サービスが止まりません")


def wait_process_exit(runner=subprocess.run, sleep=time.sleep, wait_sec: int = 10) -> None:
    """サービスが STOPPED になった後も、プロセスが少しの間ファイルを使っていることがある（削除・差し替えで
    使用中のファイルが残った。Windows の実機で発見）。終わるまで待ち、終わらなければ強制的に終わらせる"""
    tasklist = system_tool("tasklist", "Windows")
    for _ in range(wait_sec * 2):
        out = _run([tasklist, "/FI", f"IMAGENAME eq {SERVICE_EXE}", "/NH"], runner).stdout or ""
        if SERVICE_EXE.lower() not in out.lower():
            return
        sleep(0.5)
    _run([system_tool("taskkill", "Windows"), "/F", "/IM", SERVICE_EXE], runner)


def remove_service(runner=subprocess.run) -> None:
    if service_installed(runner):
        result = _sc("delete", SERVICE_NAME, runner=runner)
        if result.returncode != 0:
            raise SetupError(f"サービスの登録を消せません: {(result.stdout or '').strip()[:300]}")


# -- まとめ（clamqtpie-ctl setup） ----------------------------------------------------------------
def setup_install(data_dir: Path, app_dir: Path, user: str, out, runner=subprocess.run, now=None, check=None) -> bool:
    """導入・更新。user をグループに新しく入れたら True（サインインし直すまでトレイGUI が使えない）"""
    moved = set_aside_untrusted(data_dir, now, check)
    if moved is not None:
        print(f"一般の利用者が作った・書き換えられるデータのフォルダを退避しました: {moved}", file=out)
    from core.service_access import add_user, ensure_group, group_name

    ensure_group(runner)
    added = False
    if user:
        added = add_user(user, runner)
        if added:
            print(f"{user} を {group_name()} に入れました（サインアウトしてサインインし直すと、トレイGUI を使えます）",
                  file=out)
        else:
            print(f"{user} はすでに {group_name()} に入っています", file=out)
    stop_service(runner)
    register_service(app_dir, runner)
    start_service(runner)
    print("サービスを登録して開始しました", file=out)
    return added


def setup_uninstall(data_dir: Path, purge: bool, out, runner=subprocess.run) -> None:
    stop_service(runner)
    remove_service(runner)
    print("サービスの登録を消しました", file=out)
    from core.web_lan import close_firewall

    close_firewall(runner)  # WebUI の LAN からの接続の規則（サービスが止まるときにも消すが、残っていれば）
    if purge:
        if data_dir.exists() and not data_dir.is_symlink():
            shutil.rmtree(data_dir)
        from core.service_access import WINDOWS_GROUP

        _run([system_tool("net", "Windows"), "localgroup", WINDOWS_GROUP, "/delete"], runner)
        print("設定・データ・ログとグループを削除しました", file=out)
