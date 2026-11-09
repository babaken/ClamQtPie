"""
ClamQtPie の管理用コマンド clamqtpie-ctl（サービス化・Web API設計書 3.4節。段階 12-3b）

GUI のない環境でも、サービスの状態の確認と、Web API からは変えられない設定（保護された項目）の変更、
元の場所以外への隔離ファイルの復元を行う。root（Windows は管理者）で実行する。

使い方:
    clamqtpie-ctl status
    clamqtpie-ctl config show
    clamqtpie-ctl config set KEY VALUE [--yes] [--restart]
    clamqtpie-ctl quarantine restore ID --to PATH [--overwrite]
    clamqtpie-ctl migrate --from DIR [--yes] [--restart]
    clamqtpie-ctl web status | enable [--for D | --until T] | timer (D | --until T | clear) | disable
    clamqtpie-ctl web set-login [--username NAME] [--generate | --password-stdin] | hash-password
    clamqtpie-ctl web totp setup | status | reset [--yes]
    clamqtpie-ctl web unlock

- config set は、変更の前後の値を表示して確認を求める（--yes で省略）。サービスが動いている間は変えない
  （サービスが保存するときに上書きされるため）。--restart を付けると、サービスを止めて変え、また開始する
- 実行ファイル・設定ファイルの場所（update.freshclam_path など）は、設定するときに場所を確かめる
  （絶対パス、管理者・root だけが書き込める場所、想定どおりの名前。実行の直前にも確かめる）
- 開発中の確認では --data-dir DIR で、サービスの --console --data-dir と同じ置き場所を使える（root でなくてよい）
- migrate は、旧版（ポータブル）のフォルダの設定・DB・隔離ファイル・ログを、サービスの置き場所へ移す
  （旧版のフォルダの中身は信頼しない。core/portable_migration.py）
- web は WebUI の ON/OFF・自動で OFF にする時刻・ログインの設定（3.4節。段階 12-5a）。サービスが動いていれば
  admin のトークンで PUT /web・PUT /web/credentials を送り（セッションの破棄などをサービスに行わせる）、止まっていれば
  設定ファイルを直接変える。パスワードはコマンドラインの引数では受け取らない（ps・シェルの履歴から見えるため）
- web totp は二段階認証の設定・状態・解除（段階 12-5d）。setup は秘密鍵を作って端末に QR コードと秘密鍵を表示し、
  認証アプリのコードを入れてもらってから、秘密鍵とコードを一緒にサービスへ送る（止まっていれば設定を直接変える）。
  予備のコードは端末にだけ 1 回表示する。reset はスマートフォンをなくしたときなどに解除する
- web unlock は、確認コードの間違いが続いて止めた WebUI のログインを解く（web set-login でパスワードを変えても解ける）
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import subprocess
import sys
from dataclasses import fields
from pathlib import Path

from core.lazy_import_fix import reify_concurrent_futures

reify_concurrent_futures()

from core.config_model import GUI_SECTIONS, ConfigModel, validate_and_fix  # noqa: E402
from core.config_service import ConfigService  # noqa: E402
from core.exceptions import AppError, ServiceUnavailableError  # noqa: E402
from core.exec_path import (  # noqa: E402
    child_env, find_system_tool, no_window_kwargs, validate_executable, validate_trusted_file,
)
from core.secret_store import FileSecretStore  # noqa: E402
from core.service_endpoint import read_endpoint, verify_service_listener  # noqa: E402
from core.service_paths import ServicePaths, UnsafeDataDirectoryError, prepare_directories  # noqa: E402
from core.service_status import (  # noqa: E402
    START_LIMIT_HIT,
    SYSTEMD_UNIT,
    WINDOWS_SERVICE_NAME,
    service_running,
    service_stop_reason,
)
from core.version import APP_NAME, APP_VERSION  # noqa: E402

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_ADMIN = 3
#: setup install: 導入できたが、利用者をグループに新しく入れたため、サインインし直すまでトレイGUI を使えない
#: （インストーラが完了の画面で案内し、トレイGUI を起動しない）
EXIT_SIGNIN_REQUIRED = 10

#: 設定するときに場所を確かめる項目（実行ファイルは名前も確かめる）
_EXECUTABLE_FIELDS = {"update.freshclam_path": ("freshclam",), "realtime.clamonacc_path": ("clamonacc",)}
_TRUSTED_FILE_FIELDS = ("realtime.clamonacc_config_file",)


#: config set では変えさせず、web のコマンドで変える項目（パスワードはハッシュの作り方・長さの記録・セッションの
#: 破棄を伴い、ON/OFF はパスワードの設定済みを確かめるため）
_WEB_COMMAND_FIELDS = {
    "api.web_enabled": "web enable / web disable",
    "api.web_off_at": "web enable --for / web timer",
    "api.web_username": "web set-login --username",
    "api.web_password_hash": "web set-login",
    "api.web_password_length": "web set-login",
    "api.web_totp_enabled": "web totp setup / web totp reset",
    "api.web_totp_last_step": "web totp setup",
    "api.web_recovery_code_hashes": "web totp setup",
    "api.web_totp_failures": "web unlock",
    "api.web_tls": "web tls on / web tls off",
    "api.web_lan": "web lan on / web lan off",
}


class CtlError(Exception):
    pass


def _is_admin() -> bool:
    if sys.platform == "win32":
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    return os.geteuid() == 0


def _config_service(paths: ServicePaths) -> ConfigService:
    return ConfigService(paths.config_file, secret_store=FileSecretStore(paths.secrets_dir))


# -- status --------------------------------------------------------------------------------
def cmd_status(paths: ServicePaths, out) -> int:
    config = _config_service(paths).load()
    running = service_running()
    print(f"{APP_NAME} {APP_VERSION}", file=out)
    print(f"サービス: {'動いています' if running else '止まっています'}", file=out)
    if not running and service_stop_reason() == START_LIMIT_HIT:
        # 短い間に何度も落ちたため、systemd が起動の回数の上限で止めている。原因を確かめてから数え直して開始する
        print("  落ち続けたため、起動の回数の上限で止めています。原因はログ（journalctl -u clamqtpie・"
              "/var/log/clamqtpie/）で確かめ、直してから次を実行してください:", file=out)
        print("  sudo systemctl reset-failed clamqtpie && sudo systemctl start clamqtpie", file=out)
    # サービスが待ち受けたポート（endpoint.json）。無ければ設定の値
    port = read_endpoint(paths.api_dir) or config.api.port
    print(f"内部 API: 127.0.0.1:{port}", file=out)
    if running:
        # 待ち受けているのがサービスかを確かめてから問い合わせる（偽のサーバーに問い合わせない。12章 22）
        try:
            verify_service_listener(port)
        except ServiceUnavailableError as exc:
            print(f"  応答: 問い合わせませんでした（{exc.code}: {exc.message}。{exc.detail}）", file=out)
        else:
            print(f"  応答: {_api_version(port) or '応答がありません'}", file=out)
    from core.clamav_service import ClamAVService

    try:
        reachable = ClamAVService(config.clamd).ping(retry=False)
    except Exception:  # noqa: BLE001 - 応答しない clamd は「停止」
        reachable = False
    print(f"clamd: {'応答あり' if reachable else '応答なし'}", file=out)
    # スケジュールのためにスリープから起こせるか（Windows は電源プランの設定、Linux は設定中の予定。段階 12-3c）
    from core.wake_timer import wake_timer_policy

    print(wake_timer_policy(), file=out)
    print(f"設定: {paths.config_file}", file=out)
    print(f"データ: {paths.data_dir}", file=out)
    print(f"ログ: {paths.log_dir}", file=out)
    return EXIT_OK


def _api_version(port: int) -> str | None:
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/version", timeout=3) as response:
            data = json.loads(response.read().decode("utf-8"))
        return f"アプリ {data.get('app_version')}、API {data.get('api_version')}"
    except (OSError, ValueError):
        return None


# -- config ----------------------------------------------------------------------------------
def cmd_config_show(paths: ServicePaths, out) -> int:
    service = _config_service(paths)
    service.load()
    data = service.as_dict()
    # WebUI のパスワードのハッシュは表示しない（3.4節）。パスワードの文字数も推測の手がかりになるため出さない
    api_section = data.get("api", {})
    if api_section.get("web_password_hash"):
        api_section["web_password_hash"] = "（設定済み）"
    if api_section.get("web_password_length"):
        api_section["web_password_length"] = "（伏せています）"
    hashes = api_section.get("web_recovery_code_hashes")
    if hashes:
        api_section["web_recovery_code_hashes"] = f"（{len(hashes)} 個。伏せています）"
    print(json.dumps(data, ensure_ascii=False, indent=2), file=out)
    return EXIT_OK


def _parse_value(current, text: str):
    """文字列の値を、今の値の型に合わせて読む"""
    if isinstance(current, bool):
        if text.lower() in ("true", "1", "yes", "on"):
            return True
        if text.lower() in ("false", "0", "no", "off"):
            return False
        raise CtlError(f"true か false を指定してください: {text}")
    if isinstance(current, int):
        try:
            return int(text)
        except ValueError:
            raise CtlError(f"整数を指定してください: {text}") from None
    if isinstance(current, float):
        try:
            return float(text)
        except ValueError:
            raise CtlError(f"数を指定してください: {text}") from None
    if isinstance(current, list):
        # 一覧は JSON の配列で受け取る（例: '["/srv/share", "/home/user/Downloads"]'）
        try:
            value = json.loads(text)
        except ValueError:
            raise CtlError(f'JSON の配列で指定してください（例: \'["/path"]\'）: {text}') from None
        if not isinstance(value, list):
            raise CtlError(f"JSON の配列で指定してください: {text}")
        return value
    return text


def _default_location(key: str, value: str) -> str:
    """実行ファイルの場所は、空なら既定の名前（PATH から探す）にする"""
    if key in _EXECUTABLE_FIELDS and not value.strip():
        section, name = key.split(".")
        return getattr(getattr(ConfigModel(), section), name)
    return value


def _validate_location(key: str, value: str) -> None:
    if not value:
        return  # 空は clamonacc の既定の設定ファイル
    if key in _EXECUTABLE_FIELDS and value == _default_location(key, ""):
        return  # 既定の名前は、実行の直前に PATH から探して確かめる
    try:
        if key in _EXECUTABLE_FIELDS:
            validate_executable(value, _EXECUTABLE_FIELDS[key])
        elif key in _TRUSTED_FILE_FIELDS:
            validate_trusted_file(value)
    except AppError as exc:
        raise CtlError(f"{exc.message}（{exc.detail}）") from None


def cmd_config_set(paths: ServicePaths, key: str, text: str, yes: bool, restart: bool, out, ask=input) -> int:
    service = _config_service(paths)
    config = service.load()
    section, _, name = key.partition(".")
    target = getattr(config, section, None)
    if section in ("schema_version", *GUI_SECTIONS) or target is None or name not in {f.name for f in fields(target)}:
        raise CtlError(f"設定の項目が見つかりません: {key}")
    if key in _WEB_COMMAND_FIELDS:
        raise CtlError(f"{key} は clamqtpie-ctl web で変えてください（{_WEB_COMMAND_FIELDS[key]}）")
    before = getattr(target, name)
    value = _parse_value(before, text)
    if isinstance(value, str):
        value = _default_location(key, value)
        _validate_location(key, value)
    # 範囲外などの値は受け付けない（読み込み時のように直さない）
    trial = ConfigModel.from_dict(config.to_dict())
    setattr(getattr(trial, section), name, value)
    fixed = ConfigModel.from_dict(trial.to_dict())
    validate_and_fix(fixed, lambda _message: None)
    if getattr(getattr(fixed, section), name) != value:
        raise CtlError(f"値が範囲外か、正しくありません: {key} = {text}")
    if value == before:
        print(f"{key} は既に {before!r} です", file=out)
        return EXIT_OK

    print(f"{key}: {before!r} → {value!r}", file=out)
    if not yes and not _confirm(ask, "変更しますか？ [y/N] "):
        print("変更しませんでした", file=out)
        return EXIT_OK
    running = service_running()
    if running and not restart:
        raise CtlError("サービスが動いている間は変更できません（サービスが設定を保存するときに上書きされるため）。"
                       "--restart を付けると、サービスを止めて変更し、また開始します")
    if running:
        _control_service("stop")
    setattr(target, name, value)
    service.save(config)
    _audit(paths, f"clamqtpie-ctl: 設定を変更しました: {key}（{before!r} → {value!r}）")
    print("変更しました", file=out)
    if running:
        try:
            _control_service("start")
        except CtlError as exc:
            raise CtlError(f"変更は保存しました。{exc}") from None
        print("サービスを開始しました", file=out)
    elif key.startswith(("api.", "clamd.")):
        print("サービスの次の開始から使われます", file=out)
    return EXIT_OK


def _confirm(ask, prompt: str) -> bool:
    try:
        return ask(prompt).strip().lower() in ("y", "yes")
    except EOFError:  # 標準入力が閉じている（パイプなど）ときは「いいえ」
        return False


def _control_service(action: str) -> None:
    system = platform.system()
    if system == "Windows":
        command = [find_system_tool("sc", system) or "sc", action, WINDOWS_SERVICE_NAME]
    else:
        systemctl = find_system_tool("systemctl", system) or "systemctl"
        command = [systemctl, action, SYSTEMD_UNIT]
        if action == "start":
            # 手で行う開始も起動の回数の上限（StartLimitBurst）に数えられるため、数え直してから開始する
            subprocess.run([systemctl, "reset-failed", SYSTEMD_UNIT], capture_output=True, timeout=60,
                           env=child_env(None, system))
    result = subprocess.run(command, capture_output=True, timeout=120, env=child_env(None, system),
                            **no_window_kwargs(system))
    if result.returncode != 0:
        if action == "stop":
            raise CtlError(f"サービスを止められませんでした（{result.returncode}）")
        where = "イベントビューアーの Windows ログ（アプリケーション）" if system == "Windows" else f"journalctl -u {SYSTEMD_UNIT}"
        raise CtlError(f"サービスを開始できませんでした（{result.returncode}）。サービスは止まっています。"
                       f"原因は {where} とサービスのログで確かめてください")
    if system == "Windows" and action == "stop":
        # sc stop は止まるのを待たずに戻るため、止まりきる（STOPPED）まで待つ。止まる途中（STOP_PENDING）で
        # 次へ進むと、止まる途中のサービスが設定を上書きしたり、開始が 1056（動いている）で失敗したりする
        import time

        from core.service_status import service_stopped

        for _ in range(60):
            if service_stopped(system):
                return
            time.sleep(1)
        raise CtlError("サービスが止まりませんでした")


def _audit(paths: ServicePaths, message: str) -> None:
    """管理用コマンドで変えた内容を、サービスのログに残す"""
    from core.logger import CATEGORY_SYSTEM, log_event, setup_logger

    config = _config_service(paths).current
    setup_logger(paths.log_dir, level=config.logging.level, retention_days=config.logging.retention_days, console=False)
    log_event(CATEGORY_SYSTEM, message)


# -- quarantine ------------------------------------------------------------------------------
def cmd_quarantine_restore(paths: ServicePaths, quarantine_id: int, dest: str, overwrite: bool, out) -> int:
    from core.quarantine_service import QuarantineService
    from db.database import DatabaseService

    if not os.path.isabs(dest):
        raise CtlError(f"復元先は絶対パスで指定してください: {dest}")
    db = DatabaseService(paths.db_file, quiet=True)
    db.connect()
    try:
        service = QuarantineService(db, paths.quarantine_dir)
        row = service.get(quarantine_id)
        if row.restored or row.deleted:
            raise CtlError("既に復元または削除されています")
        service.restore(quarantine_id, dest_path=dest, overwrite=overwrite)
        keeps_owner = bool(row.owner_info)
    except AppError as exc:
        raise CtlError(f"{exc.message}（{exc.detail or exc.code}）") from None
    finally:
        db.close()
    _audit(paths, f"clamqtpie-ctl: 隔離ファイルを元の場所以外へ復元しました: id={quarantine_id} → {dest}")
    print(f"復元しました: {dest}", file=out)
    if not keeps_owner:
        # 元の所有者・アクセス権の記録が無い（旧版から移した記録など）ため、管理者・root のファイルになる
        owner = "Administrators" if sys.platform == "win32" else "root"
        print(f"復元したファイルの所有者は {owner} です。利用者が使う場合は、所有者・アクセス権を変えてください", file=out)
    return EXIT_OK


# -- migrate --------------------------------------------------------------------------------
def cmd_migrate(paths: ServicePaths, source_text: str, yes: bool, restart: bool, strict: bool, out,
                ask=input, ask_password=getpass.getpass) -> int:
    state = {"stopped": False}
    try:
        return _migrate(paths, source_text, yes, restart, strict, out, ask, ask_password, state)
    finally:
        if state["stopped"]:  # 途中で失敗しても、止めたサービスは開始し直す
            _control_service("start")
            print("サービスを開始しました", file=out)


def _migrate(paths, source_text, yes, restart, strict, out, ask, ask_password, state) -> int:
    from core import portable_migration as pm
    from core.gui_config import gui_settings_from
    from db.database import DatabaseService

    try:
        source = pm.check_source(Path(source_text), paths.data_dir)
        running = service_running()
        if running and not restart:
            raise CtlError("サービスが動いている間は移行できません。--restart を付けると、サービスを止めて移行し、また開始します")
        try:
            prepare_directories(paths, strict=strict)
        except UnsafeDataDirectoryError as exc:
            raise CtlError(str(exc)) from None
        data = pm.read_legacy(source, paths.data_dir)
        config_service = _config_service(paths)
        config, diffs, changed = pm.build_config(config_service.load(), data.config)
        gui = gui_settings_from(data.config.get("general")) if data.config else None

        db = DatabaseService(paths.db_file, quiet=True)
        db.connect()
        try:
            pm.ensure_target_empty(db)
            _print_plan(source, data, changed, out)
            if not yes and not _confirm(ask, "移行しますか？ [y/N] "):
                print("移行しませんでした", file=out)
                return EXIT_OK
            if running:
                _control_service("stop")
                state["stopped"] = True
            copied = pm.copy_quarantine_files(source, paths.quarantine_dir, data.quarantine, data.skipped)
            try:
                pm.insert_rows(db, data)
            except Exception as exc:
                pm.remove_copied(paths.quarantine_dir, copied)
                raise CtlError(f"DB に書き込めませんでした。移したものを元に戻して中止しました: {exc}") from None
        finally:
            db.close()
    except pm.MigrationError as exc:
        raise CtlError(str(exc)) from None

    config_service.save(config)
    notes = list(data.skipped)
    password = pm.legacy_smtp_password(data.config)
    if password is None and sys.platform != "win32" and config.smtp.user and not yes:
        # Linux は sudo では利用者の Secret Service を読めないため、入力してもらう（空なら後で設定画面から）
        password = ask_password("SMTP のパスワード（空のまま Enter で省略。後で設定画面から入力できます）: ") or None
    if password:
        config_service.set_smtp_password(password)
        print("SMTP のパスワードを移しました", file=out)
    notes += pm.copy_logs(source, paths.log_dir)
    if gui is not None:
        try:
            print(f"GUI の設定を書き出しました: {pm.write_gui_settings(gui)}", file=out)
        except pm.MigrationError as exc:
            notes.append(f"GUI の設定: {exc}")
    notes += pm.remove_legacy_quarantine(source, copied)
    try:
        pm.write_marker(source, paths.data_dir)
    except OSError as exc:
        notes.append(f"移行済みの印（{pm.MARKER_NAME}）を置けませんでした: {exc}")
    _audit(paths, f"clamqtpie-ctl: 旧版のデータを移しました: {source}（隔離 {len(data.quarantine)} 件、"
                  f"スケジュール {len(data.schedules)} 件、除外 {len(data.excludes)} 件、監視対象 {len(data.watch_targets)} 件）")
    print("移行しました", file=out)
    if notes:
        print("移さなかったもの・注意:", file=out)
        for note in notes:
            print(f"  - {note}", file=out)
    if diffs:
        print("次の項目は、安全のため旧版の値を移さず、今の値のままにしました。"
              "必要なら clamqtpie-ctl config set で設定し直してください:", file=out)
        for key, value in diffs:
            print(f"  - {key}（旧版: {value!r}）", file=out)
    return EXIT_OK


def _print_plan(source: Path, data, changed: list[str], out) -> None:
    active = [r for r in data.quarantine if not (r["restored"] or r["deleted"])]
    print(f"旧版のフォルダ: {source}", file=out)
    print(f"設定: {len(changed)} 項目を移します" + (f"（{', '.join(changed)}）" if changed else ""), file=out)
    print(f"除外: {len(data.excludes)} 件", file=out)
    for row in data.excludes:
        print(f"  - [{row['type']}] {row['value']}{'' if row.get('enabled') else '（無効）'}", file=out)
    print(f"監視対象: {len(data.watch_targets)} 件", file=out)
    for row in data.watch_targets:
        print(f"  - {row['path']}{'' if row.get('enabled') else '（無効）'}", file=out)
    print(f"スケジュール: {len(data.schedules)} 件", file=out)
    for row in data.schedules:
        target = f" {row['target']}" if row["target"] else ""
        print(f"  - {row['name']}: {row['scan_type']} {row['cron']}{target}{'' if row.get('enabled') else '（無効）'}",
              file=out)
    print(f"隔離中のファイル: {len(active)} 件", file=out)
    for row in active:
        print(f"  - {row['original_path']}（{row['virus_name']}）", file=out)
    print(f"スキャンの履歴: {len(data.scan_history)} 件、定義の更新の履歴: {len(data.update_history)} 件", file=out)


# -- setup（Windows のインストーラ） -----------------------------------------------------------
def cmd_setup(paths: ServicePaths, action: str, user: str, purge: bool, out, runner=subprocess.run) -> int:
    """Windows のインストーラ（packaging/windows/ClamQtPie.iss）から呼ぶ。Linux は install.sh・uninstall.sh"""
    if sys.platform != "win32":
        raise CtlError("setup は Windows のインストーラから使います（Linux は install.sh・uninstall.sh）")
    from core.service_access import AccessError
    from core.windows_setup import SetupError, setup_install, setup_uninstall

    try:
        if action == "install":
            added = setup_install(paths.data_dir, Path(sys.executable).resolve().parent, user, out, runner)
            _audit(paths, f"clamqtpie-ctl: 導入しました（{APP_NAME} {APP_VERSION}）")
            if added:
                return EXIT_SIGNIN_REQUIRED
        else:
            setup_uninstall(paths.data_dir, purge, out, runner)
    except (SetupError, AccessError, OSError) as exc:
        raise CtlError(str(exc)) from exc
    return EXIT_OK


# -- users --------------------------------------------------------------------------------
def cmd_users(paths: ServicePaths, action: str, name: str, out, runner=subprocess.run) -> int:
    """トレイGUI を使える利用者（サービス化・Web API設計書 3.2節）。トレイGUI を使えるのは管理者だけにする"""
    from core.service_access import AccessError, add_user, group_name, list_users, remove_user

    try:
        if action == "list":
            members = list_users(runner)
            print(f"{group_name()} の利用者: {', '.join(members) if members else '（なし）'}", file=out)
        elif action == "add":
            if not add_user(name, runner):
                print(f"{name} はすでに {group_name()} に入っています", file=out)
                return EXIT_OK
            _audit(paths, f"clamqtpie-ctl: トレイGUI を使える利用者に加えました: {name}")
            print(f"{name} を {group_name()} に入れました。サインアウトしてサインインし直すと、トレイGUI を使えます"
                  "（使えなければ PC を再起動してください）", file=out)
        else:
            remove_user(name, runner)
            _audit(paths, f"clamqtpie-ctl: トレイGUI を使える利用者から外しました: {name}")
            print(f"{name} を {group_name()} から外しました。動いているトレイGUI は、サービスを起動し直すと"
                  "つながらなくなります", file=out)
    except AccessError as exc:
        raise CtlError(str(exc)) from exc
    return EXIT_OK


# -- web（3.4節。段階 12-5a） ----------------------------------------------------------------
def _web_api(paths: ServicePaths):
    """サービスが動いていれば admin のトークンで内部 API を呼ぶもの（接続先がサービスかを確かめてから）。止まっていれば None"""
    if not service_running():
        return None
    from elevate_main import AdminApi

    try:
        return AdminApi(paths.api_dir)
    except (AppError, OSError) as exc:
        detail = f"{exc.message}。{exc.detail or ''}" if isinstance(exc, AppError) else str(exc)
        raise CtlError(f"サービスに接続できません（{detail}）") from None


def _web_call(api, method: str, path: str, body=None):
    try:
        return api.call(method, path, body)
    except AppError as exc:
        raise CtlError(f"{exc.message}{'（' + exc.detail + '）' if exc.detail else ''}") from None
    except OSError as exc:
        raise CtlError(f"サービスに接続できません（{exc}）") from None


def _web_offline_save(paths: ServicePaths, changes: dict, message: str, out) -> None:
    service = _config_service(paths)
    config = service.load()
    for name, value in changes.items():
        setattr(config.api, name, value)
    service.save(config)
    _audit(paths, f"clamqtpie-ctl: {message}")
    print("サービスは止まっています。設定ファイルを変えました（次の開始から使われます）", file=out)


def _web_update(paths: ServicePaths, body: dict, out, message: str | None = None) -> dict:
    """PUT /web（サービスが止まっていれば設定ファイルを変える。message はそのときの監査ログの文）"""
    from api.web_control import web_changes, web_state_text
    from core.exceptions import ConfigValidationError

    api = _web_api(paths)
    if api is not None:
        return _web_call(api, "PUT", "/web", body)
    config = _config_service(paths).load()
    try:
        changes = web_changes(config.api, body)
    except ConfigValidationError as exc:
        raise CtlError(exc.message) from None
    for name, value in changes.items():
        setattr(config.api, name, value)
    _web_offline_save(paths, changes, message or f"WebUI を {web_state_text(config.api)} にしました", out)
    return _web_status_offline(config.api)


def _login_stopped(api_config) -> bool:
    from api.web_control import login_stopped

    return login_stopped(api_config)


_STOPPED_TEXT = ("止めています（パスワードは合っていて確認コードだけが違うログインが続いたため。パスワードが漏れている"
                 "おそれがあります。web set-login でパスワードを変えるか、web unlock で解けます。予備のコードなら"
                 "ログインできます）")


def _web_status_offline(api_config) -> dict:
    scheme = "https" if api_config.web_tls != "off" else "http"
    return {"enabled": api_config.web_enabled, "running": False, "lan": bool(api_config.web_lan),
            "lan_listening": False, "lan_notes": [],
            "urls": [f"{scheme}://127.0.0.1:{api_config.web_port}/web/login"], "username": api_config.web_username,
            "password_set": bool(api_config.web_password_hash), "totp_set": bool(api_config.web_totp_enabled),
            "recovery_codes_left": len(api_config.web_recovery_code_hashes) if api_config.web_totp_enabled else 0,
            "login_stopped": _login_stopped(api_config),
            "tls": api_config.web_tls,
            "off_at": api_config.web_off_at or None, "session_timeout_min": api_config.session_timeout_min}


def _print_web_status(status: dict, out) -> None:
    from core.duration_text import local_time_text

    state = "ON" if status.get("enabled") else "OFF"
    if status.get("enabled") and not status.get("running"):
        state += "（いまは待ち受けていません）"
    print(f"WebUI: {state}", file=out)
    off_at = status.get("off_at")
    print(f"OFF にする時刻: {local_time_text(off_at) if off_at else 'なし（OFF にするまで）'}", file=out)
    if status.get("enabled"):  # OFF のときはつながらない URL を出さない
        for url in status.get("urls", []):
            print(f"URL: {url}", file=out)
    print(f"ユーザー名: {status.get('username')}", file=out)
    print(f"パスワード: {'設定済み' if status.get('password_set') else '未設定（web set-login で設定してください）'}",
          file=out)
    if status.get("totp_set"):
        print(f"二段階認証: 設定済み（予備のコードの残り: {status.get('recovery_codes_left', 0)} 個）", file=out)
    else:
        print("二段階認証: 未設定（web totp setup で設定できます）", file=out)
    if status.get("login_stopped"):
        print(f"ログイン: {_STOPPED_TEXT}", file=out)
    tls = status.get("tls", "off")
    print(f"HTTPS: {'無効' if tls == 'off' else '有効（ClamQtPie のローカル CA。web tls status で詳しく）'}", file=out)
    for note in (status.get("tls_info") or {}).get("notes", []):
        print(f"  注意: {note}", file=out)
    if not status.get("lan"):
        print("LAN からの接続: 許可しない（このPC のブラウザだけ）", file=out)
        return
    if status.get("lan_listening"):
        print("LAN からの接続: 許可（試験的。LAN の端末からも上の URL で開けます）", file=out)
    else:
        print("LAN からの接続: 許可（試験的。WebUI を ON にしてサービスが動いていると、LAN からも待ち受けます）", file=out)
    for note in status.get("lan_notes") or []:
        print(f"  注意: {note}", file=out)


def _read_new_password(generate: bool, from_stdin: bool, out, ask_password, read_line) -> str:
    from core.web_password import generate_password, password_problem

    if generate:
        password = generate_password()
    elif from_stdin:
        password = read_line().rstrip("\r\n")
    else:
        password = ask_password("新しいパスワード: ")
        if ask_password("もう一度入力してください: ") != password:
            raise CtlError("パスワードが一致しません")
    problem = password_problem(password)
    if problem:
        raise CtlError(problem)
    return password


def _print_qr(uri: str, out) -> None:
    """端末に QR コードを表示する（表示できない文字コードの端末では、秘密鍵の手入力だけを案内する）"""
    from core.totp import qr_text

    text = qr_text(uri)
    encoding = getattr(out, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        print("（この端末では QR コードを表示できません。下の秘密鍵を認証アプリに手で入れてください）", file=out)
        return
    print(text, file=out)


def cmd_web_totp(paths: ServicePaths, args, out, ask=input) -> int:
    from api.web_control import disable_totp, enable_totp
    from core import totp
    from core.exceptions import ConfigValidationError

    action = args.totp_action
    config = _config_service(paths).load().api
    if action == "status":
        api = _web_api(paths)
        status = _web_call(api, "GET", "/web") if api is not None else _web_status_offline(config)
        if status.get("totp_set"):
            print(f"二段階認証: 設定済み（予備のコードの残り: {status.get('recovery_codes_left', 0)} 個）", file=out)
        else:
            print("二段階認証: 未設定", file=out)
        return EXIT_OK
    if action == "reset":
        if not config.web_totp_enabled:
            print("二段階認証は設定されていません", file=out)
            return EXIT_OK
        if not args.yes and ask("二段階認証を解除します。ログイン中の WebUI はすべてログアウトします。"
                                "よろしいですか [y/N]: ").strip().lower() != "y":
            print("取り消しました", file=out)
            return EXIT_ERROR
        api = _web_api(paths)
        if api is not None:
            _web_call(api, "DELETE", "/web/totp")
        else:
            disable_totp(_config_service(paths))
            _audit(paths, "clamqtpie-ctl: WebUI の二段階認証を解除しました")
            print("サービスは止まっています。設定ファイルを変えました（次の開始から使われます）", file=out)
        print("WebUI の二段階認証を解除しました", file=out)
        _print_lan_turned_off(paths, config.web_lan, out)
        return EXIT_OK
    if action == "setup":
        if config.web_totp_enabled and ask(
                "二段階認証は設定済みです。設定し直すと、今の認証アプリの登録と予備のコードは使えなくなります。"
                "続けますか [y/N]: ").strip().lower() != "y":
            print("取り消しました", file=out)
            return EXIT_ERROR
        secret = totp.generate_secret()
        uri = totp.otpauth_uri(secret, totp.machine_name())
        print("スマートフォンの認証アプリ（Google Authenticator・Microsoft Authenticator など）で、次の QR コードを"
              "読み取ってください。", file=out)
        _print_qr(uri, out)
        print(f"手で入れるときの秘密鍵: {' '.join(secret[i:i + 4] for i in range(0, len(secret), 4))}", file=out)
        code = ask("認証アプリに表示された 6 桁のコード: ").strip()
        api = _web_api(paths)
        if api is not None:
            codes = _web_call(api, "POST", "/web/totp", {"secret": secret, "code": code})["recovery_codes"]
        else:
            try:
                codes = enable_totp(_config_service(paths), secret, code)
            except ConfigValidationError as exc:
                raise CtlError(exc.message) from None
            _audit(paths, "clamqtpie-ctl: WebUI の二段階認証を設定しました")
            print("サービスは止まっています。設定ファイルを変えました（次の開始から使われます）", file=out)
        print("WebUI の二段階認証を設定しました。", file=out)
        # 予備のコードは端末にだけ 1 回表示する（ログには残さない）
        print("予備のコード（スマートフォンをなくしたときに、確認コードの代わりに 1 個ずつ使えます。"
              "この画面にだけ表示します。控えてください）:", file=out)
        for item in codes:
            print(f"  {item}", file=out)
        return EXIT_OK
    return EXIT_ERROR


# -- web tls（段階 12-5e1） --------------------------------------------------------------------------
_REGISTER_HINT = ("ブラウザを使う端末（この PC を含む）に CA 証明書を登録すると、警告なしで開けます。web tls export-ca で"
                  "書き出すか、WebUI のログイン画面からダウンロードし、フィンガープリントがこの表示と同じことを確かめてから"
                  "登録してください（手順は README）")


def _print_lan_turned_off(paths: ServicePaths, lan_before: bool, out) -> None:
    """操作（HTTPS の無効化・二段階認証の解除・短いパスワード）で LAN からの接続の許可が OFF になったら知らせる"""
    if lan_before and not _config_service(paths).load().api.web_lan:
        print("LAN からの接続の許可も OFF にしました（条件を満たさなくなったため）", file=out)


def cmd_web_lan(paths: ServicePaths, allow: bool, out) -> int:
    """web lan on|off（段階 12-5e2。試験的）。許可の条件（HTTPS・二段階認証・12 文字以上のパスワード）は
    サービス（止まっていればここ）が確かめる"""
    state = "許可しました（試験的）" if allow else "やめました"
    status = _web_update(paths, {"lan": allow}, out, f"WebUI の LAN からの接続を{state}")
    print(f"WebUI の LAN からの接続を{state}（ログイン中の WebUI はログアウトします）", file=out)
    _print_web_status(status, out)
    if allow:
        print("LAN の端末では、WebUI のログイン画面から CA 証明書をダウンロードし、フィンガープリントが web tls status の"
              "表示と同じことを確かめてから登録してください（手順は README）。LAN から待ち受けている間のセッションの"
              "有効期限は 15 分です", file=out)
    return EXIT_OK


def _local_ca(paths: ServicePaths):
    from core.local_ca import LocalCa

    return LocalCa(paths.tls_dir)


def _print_tls_status(paths: ServicePaths, mode: str, out) -> None:
    print(f"HTTPS: {'無効' if mode == 'off' else '有効（ClamQtPie のローカル CA）'}", file=out)
    status = _local_ca(paths).status()
    if status.ca_fingerprint is None:
        if mode != "off":
            print("CA 証明書: まだ作っていません（サービスが WebUI を HTTPS で待ち受けるときに作ります）", file=out)
        return
    print(f"CA 証明書の SHA-256 のフィンガープリント: {status.ca_fingerprint}", file=out)
    print(f"CA 証明書の期限: {status.ca_not_after:%Y-%m-%d}", file=out)
    if status.server_not_after is not None:
        print(f"サーバー証明書の期限: {status.server_not_after:%Y-%m-%d}（自動で作り直します）", file=out)
        print(f"サーバー証明書の名前: {', '.join(status.server_names)}", file=out)
    for note in status.notes:
        print(f"注意: {note}", file=out)


def cmd_web_tls(paths: ServicePaths, args, out, ask=input) -> int:
    from api.web_control import tls_changes
    from core.exceptions import ConfigValidationError

    action = args.tls_action
    if action == "status":
        _print_tls_status(paths, _config_service(paths).load().api.web_tls, out)
        return EXIT_OK
    if action == "export-ca":
        pem = _local_ca(paths).ca_pem()
        if pem is None:
            raise CtlError("CA 証明書がありません（web tls on で HTTPS を有効にし、サービスが WebUI を待ち受けると作ります）")
        if args.file:
            Path(args.file).write_bytes(pem)
            print(f"CA 証明書を書き出しました: {args.file}", file=out)
            print(f"SHA-256 のフィンガープリント: {_local_ca(paths).status().ca_fingerprint}", file=out)
        else:
            out.write(pem.decode("ascii"))
        return EXIT_OK
    if action in ("on", "off"):
        mode = "local_ca" if action == "on" else "off"
        lan_before = _config_service(paths).load().api.web_lan
        try:
            changes = tls_changes(mode)
        except ConfigValidationError as exc:
            raise CtlError(exc.message) from None
        api = _web_api(paths)
        if api is not None:
            status = _web_call(api, "PUT", "/web/tls", {"mode": mode})
        else:
            _web_offline_save(paths, changes, f"WebUI の HTTPS を{'有効' if mode != 'off' else '無効'}にしました", out)
            status = _web_status_offline(_config_service(paths).load().api)
        print(f"WebUI の HTTPS を{'有効' if mode != 'off' else '無効'}にしました（ログイン中の WebUI はログアウトします）",
              file=out)
        _print_lan_turned_off(paths, lan_before, out)
        _print_tls_status(paths, mode, out)
        # http と https が入れ替わるので、開き直す URL を出す（WebUI が OFF のときはつながらないので出さない）
        if status.get("enabled"):
            for url in status.get("urls", []):
                print(f"URL: {url}（開いている WebUI はこの URL で開き直してください）", file=out)
        if mode != "off":
            print(_REGISTER_HINT, file=out)
        return EXIT_OK
    if action == "renew":
        api = _web_api(paths)
        if api is not None:
            _web_call(api, "POST", "/web/tls/renew")
        else:
            try:
                _local_ca(paths).remove_server()
            except OSError as exc:
                raise CtlError(f"サーバー証明書を消せませんでした（{exc}）") from None
            _audit(paths, "clamqtpie-ctl: WebUI の HTTPS のサーバー証明書を作り直しました（CA はそのまま）")
            print("サービスは止まっています。サーバー証明書を消しました（HTTPS が有効なら、次の開始で作り直します）", file=out)
        print("サーバー証明書を作り直しました（CA はそのままなので、端末への CA の登録はやり直さなくてかまいません。"
              "警告を越えて例外を作った端末も、開き直すと CA で確かめられます）", file=out)
        _print_tls_status(paths, _config_service(paths).load().api.web_tls, out)
        return EXIT_OK
    if action == "reset-ca":
        old = _local_ca(paths).status().ca_fingerprint
        if not args.yes:
            print("ローカル CA を作り直します。すべての端末で、新しい CA 証明書の登録のやり直しが要ります。", file=out)
            if old:
                print("ローカル CA は失効させられないため、各端末から前の CA 証明書を削除してください"
                      f"（前の CA の SHA-256 のフィンガープリント: {old}）", file=out)
            if not _confirm(ask, "作り直しますか？ [y/N]: "):
                print("やめました", file=out)
                return EXIT_OK
        api = _web_api(paths)
        if api is not None:
            _web_call(api, "POST", "/web/tls/reset-ca")
        else:
            try:
                _local_ca(paths).remove()
            except OSError as exc:
                raise CtlError(f"CA を消せませんでした（{exc}）") from None
            _audit(paths, f"clamqtpie-ctl: WebUI のローカル CA を作り直しました（前の CA: {old or 'なし'}）")
            print("サービスは止まっています。CA を消しました（HTTPS が有効なら、次の開始で作り直します）", file=out)
        print("ローカル CA を作り直しました", file=out)
        if old:
            print(f"各端末から前の CA 証明書を削除してください（SHA-256 のフィンガープリント: {old}）", file=out)
        _print_tls_status(paths, _config_service(paths).load().api.web_tls, out)
        return EXIT_OK
    return EXIT_ERROR


def _web_unlock(paths: ServicePaths, out) -> int:
    api = _web_api(paths)
    if api is not None:
        was_stopped = _web_call(api, "GET", "/web").get("login_stopped")
        _web_call(api, "POST", "/web/unlock")
    else:
        from api.web_control import unlock_login

        was_stopped = unlock_login(_config_service(paths))
        _audit(paths, "clamqtpie-ctl: WebUI のログインの停止を解きました")
        print("サービスは止まっています。設定ファイルを変えました（次の開始から使われます）", file=out)
    print("WebUI のログインの停止を解きました" if was_stopped
          else "WebUI のログインは止めていません（確認コードの間違いの回数を 0 に戻しました）", file=out)
    return EXIT_OK


def cmd_web(paths: ServicePaths, args, out, ask_password=getpass.getpass, read_line=None, ask=input) -> int:
    from core.duration_text import parse_duration_minutes, parse_until

    if read_line is None:
        # --password-stdin でも、端末で手入力するときは、入力した文字を画面に出さない
        read_line = (lambda: ask_password("新しいパスワード: ")) if sys.stdin.isatty() else sys.stdin.readline
    action = args.web_action
    if action == "totp":
        return cmd_web_totp(paths, args, out, ask)
    if action == "tls":
        return cmd_web_tls(paths, args, out, ask)
    if action == "lan":
        return cmd_web_lan(paths, args.lan_action == "on", out)
    try:
        if action == "status":
            api = _web_api(paths)
            status = _web_call(api, "GET", "/web") if api is not None else \
                _web_status_offline(_config_service(paths).load().api)
            _print_web_status(status, out)
            return EXIT_OK
        if action == "enable":
            body = {"enabled": True}
            if args.duration:
                body["off_after_min"] = parse_duration_minutes(args.duration)
            elif args.until:
                body["off_at"] = parse_until(args.until)
            else:
                body["off_after_min"] = None
            _print_web_status(_web_update(paths, body, out), out)
            return EXIT_OK
        if action == "timer":
            if not _config_service(paths).load().api.web_enabled:
                raise CtlError("WebUI は OFF です（先に web enable を実行してください）")
            if args.until:
                body = {"off_at": parse_until(args.until)}
            elif args.duration == "clear":
                body = {"off_after_min": None}
            elif args.duration:
                body = {"off_after_min": parse_duration_minutes(args.duration)}
            else:
                raise CtlError("長さ（例: 8h）か --until か clear を指定してください")
            _print_web_status(_web_update(paths, body, out), out)
            return EXIT_OK
        if action == "disable":
            _print_web_status(_web_update(paths, {"enabled": False}, out), out)
            return EXIT_OK
        if action == "unlock":
            return _web_unlock(paths, out)
    except ValueError as exc:
        raise CtlError(str(exc)) from None

    if action == "set-login":
        from api.web_control import credential_changes
        from core.exceptions import ConfigValidationError

        before = _config_service(paths).load().api
        username = args.username or before.web_username
        password = _read_new_password(args.generate, args.password_stdin, out, ask_password, read_line)
        api = _web_api(paths)
        if api is not None:
            _web_call(api, "PUT", "/web/credentials", {"username": username, "password": password})
        else:
            try:
                changes = credential_changes(username, password)
            except ConfigValidationError as exc:
                raise CtlError(exc.message) from None
            _web_offline_save(paths, changes, f"WebUI のログインを設定しました（ユーザー名: {username}）", out)
        print(f"WebUI のログインを設定しました（ユーザー名: {username}）", file=out)
        _print_lan_turned_off(paths, before.web_lan, out)
        if args.generate:
            # 生成したパスワードは端末にだけ 1 回表示する（ログには残さない）
            print(f"生成したパスワード（この画面にだけ表示します。控えてください）: {password}", file=out)
        return EXIT_OK
    if action == "hash-password":
        from core.web_password import hash_password

        password = _read_new_password(False, args.password_stdin, out, ask_password, read_line)
        print(hash_password(password), file=out)
        return EXIT_OK
    return EXIT_ERROR


# -- 入口 ------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clamqtpie-ctl", description=f"{APP_NAME} の管理用コマンド")
    parser.add_argument("--data-dir", help="開発中の確認用: サービスの --console --data-dir と同じ置き場所を使う")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="サービスの状態を表示する")
    config = sub.add_parser("config", help="設定を表示・変更する").add_subparsers(dest="action", required=True)
    config.add_parser("show", help="設定を表示する")
    setter = config.add_parser("set", help="設定の項目を変える（保護された項目を含む）")
    setter.add_argument("key")
    setter.add_argument("value")
    setter.add_argument("--yes", action="store_true", help="確認を省く")
    setter.add_argument("--restart", action="store_true", help="サービスが動いていれば、止めて変更し、また開始する")
    quarantine = sub.add_parser("quarantine", help="隔離ファイル").add_subparsers(dest="action", required=True)
    restore = quarantine.add_parser("restore", help="元の場所以外へ復元する")
    restore.add_argument("id", type=int)
    restore.add_argument("--to", required=True, dest="dest")
    restore.add_argument("--overwrite", action="store_true", help="同じ名前のファイルがあれば上書きする")
    users = sub.add_parser("users", help="トレイGUI を使える利用者（ClamQtPie Users・clamqtpie グループ）") \
        .add_subparsers(dest="action", required=True)
    users.add_parser("list", help="グループの利用者を表示する")
    users.add_parser("add", help="利用者をグループに入れる").add_argument("name")
    users.add_parser("remove", help="利用者をグループから外す").add_argument("name")
    setup = sub.add_parser("setup", help="導入・削除（Windows のインストーラから呼ぶ）") \
        .add_subparsers(dest="action", required=True)
    setup_install = setup.add_parser("install", help="データの置き場所の確認・グループ・サービスの登録と開始")
    setup_install.add_argument("--user", default="", help="ClamQtPie Users に入れる利用者（導入した人）")
    setup.add_parser("uninstall", help="サービスを止めて登録を消す").add_argument(
        "--purge", action="store_true", help="設定・データ・ログとグループも消す")
    migrate = sub.add_parser("migrate", help="旧版（ポータブル）のデータを移す")
    migrate.add_argument("--from", required=True, dest="source", help="旧版のフォルダ（ClamQtPie.exe・app.py のあるところ）")
    migrate.add_argument("--yes", action="store_true", help="確認を省く")
    migrate.add_argument("--restart", action="store_true", help="サービスが動いていれば、止めて移行し、また開始する")
    web = sub.add_parser("web", help="WebUI の ON/OFF・ログインの設定").add_subparsers(dest="web_action", required=True)
    web.add_parser("status", help="WebUI の状態を表示する")
    enable = web.add_parser("enable", help="WebUI を ON にする")
    when = enable.add_mutually_exclusive_group()
    when.add_argument("--for", dest="duration", help="この長さの後に OFF にする（例: 30m、8h、2d、1d12h、1y）")
    when.add_argument("--until", help="この日時に OFF にする（例: 23:00、2026-11-01T18:00。ローカル時刻）")
    timer = web.add_parser("timer", help="ON のまま、OFF にする時刻を変える・取り消す")
    timer.add_argument("duration", nargs="?", help="長さ（例: 8h）か clear")
    timer.add_argument("--until", help="この日時に OFF にする（例: 23:00）")
    web.add_parser("disable", help="WebUI を OFF にする")
    login = web.add_parser("set-login", help="ユーザー名とパスワードを設定する（パスワードは画面に表示しない）")
    login.add_argument("--username", help="ユーザー名（省略時は今のユーザー名）")
    source = login.add_mutually_exclusive_group()
    source.add_argument("--generate", action="store_true", help="パスワードを自動生成し、1 回だけ表示する")
    source.add_argument("--password-stdin", action="store_true", help="パスワードを標準入力から 1 行読む")
    hashing = web.add_parser("hash-password", help="パスワードの保存形式のハッシュだけを出力する（設定は変えない）")
    hashing.add_argument("--password-stdin", action="store_true", help="パスワードを標準入力から 1 行読む")
    web.add_parser("unlock", help="確認コードの間違いが続いて止めた WebUI のログインを解く")
    totp_parser = web.add_parser("totp", help="二段階認証（認証アプリの確認コード）の設定・状態・解除")
    totp_actions = totp_parser.add_subparsers(dest="totp_action", required=True)
    totp_actions.add_parser("setup", help="二段階認証を設定する（QR コードを表示し、コードを入れて確かめる）")
    totp_actions.add_parser("status", help="二段階認証の状態を表示する")
    reset = totp_actions.add_parser("reset", help="二段階認証を解除する（スマートフォンをなくしたときなど）")
    reset.add_argument("--yes", action="store_true", help="確認を省く")
    lan_parser = web.add_parser("lan", help="LAN からの接続（試験的。HTTPS・二段階認証・12 文字以上のパスワードが要る）")
    lan_actions = lan_parser.add_subparsers(dest="lan_action", required=True)
    lan_actions.add_parser("on", help="LAN からの接続を許可する")
    lan_actions.add_parser("off", help="LAN からの接続をやめる（このPC のブラウザだけにする）")
    tls_parser = web.add_parser("tls", help="WebUI の HTTPS（ClamQtPie のローカル CA）")
    tls_actions = tls_parser.add_subparsers(dest="tls_action", required=True)
    tls_actions.add_parser("on", help="HTTPS を有効にする（初回はローカル CA を作る）")
    tls_actions.add_parser("off", help="HTTPS を無効にする")
    tls_actions.add_parser("status", help="CA とサーバー証明書の期限・名前・フィンガープリントを表示する")
    export = tls_actions.add_parser("export-ca", help="CA 証明書を書き出す（省略時は標準出力へ PEM）")
    export.add_argument("file", nargs="?", help="書き出すファイル")
    tls_actions.add_parser("renew", help="サーバー証明書だけを作り直す（CA はそのまま。端末に残った証明書の例外を使わせなくする）")
    reset_ca = tls_actions.add_parser("reset-ca", help="ローカル CA を作り直す（全端末で登録のやり直しが要る）")
    reset_ca.add_argument("--yes", action="store_true", help="確認を省く")
    return parser


def main(argv: list[str] | None = None, out=sys.stdout, ask=input, ask_password=getpass.getpass) -> int:
    args = build_parser().parse_args(argv)
    if args.data_dir:
        paths = ServicePaths.under(args.data_dir)
    else:
        if not _is_admin():
            print("clamqtpie-ctl は root（Windows は管理者）で実行してください", file=sys.stderr)
            return EXIT_NOT_ADMIN
        paths = ServicePaths.default()
    try:
        if args.command == "status":
            return cmd_status(paths, out)
        if args.command == "config" and args.action == "show":
            return cmd_config_show(paths, out)
        if args.command == "config" and args.action == "set":
            return cmd_config_set(paths, args.key, args.value, args.yes, args.restart, out, ask)
        if args.command == "quarantine" and args.action == "restore":
            return cmd_quarantine_restore(paths, args.id, args.dest, args.overwrite, out)
        if args.command == "setup":
            return cmd_setup(paths, args.action, getattr(args, "user", ""), getattr(args, "purge", False), out)
        if args.command == "users":
            return cmd_users(paths, args.action, getattr(args, "name", ""), out)
        if args.command == "migrate":
            return cmd_migrate(paths, args.source, args.yes, args.restart, not args.data_dir, out, ask, ask_password)
        if args.command == "web":
            return cmd_web(paths, args, out, ask_password, ask=ask)
    except CtlError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
