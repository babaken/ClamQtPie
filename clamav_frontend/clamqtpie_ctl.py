"""
ClamQtPie の管理用コマンド clamqtpie-ctl（サービス化・Web API設計書 3.4節。段階 12-3b）

GUI のない環境でも、サービスの状態の確認と、Web API からは変えられない設定（保護された項目）の変更、
元の場所以外への隔離ファイルの復元を行う。root（Windows は管理者）で実行する。

使い方:
    clamqtpie-ctl status
    clamqtpie-ctl config show
    clamqtpie-ctl config set KEY VALUE [--yes] [--restart]
    clamqtpie-ctl quarantine restore ID --to PATH [--overwrite]

- config set は、変更の前後の値を表示して確認を求める（--yes で省略）。サービスが動いている間は変えない
  （サービスが保存するときに上書きされるため）。--restart を付けると、サービスを止めて変え、また開始する
- 実行ファイル・設定ファイルの場所（update.freshclam_path など）は、設定するときに場所を確かめる
  （絶対パス、管理者・root だけが書き込める場所、想定どおりの名前。実行の直前にも確かめる）
- 開発中の確認では --data-dir DIR で、サービスの --console --data-dir と同じ置き場所を使える（root でなくてよい）
- WebUI の設定（web ...）は段階 12-5、トレイGUI を使える利用者の追加（users ...）は段階 12-4、
  旧版のデータの移行（migrate）は段階 12-3b の後半で作る
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from dataclasses import fields

from core.lazy_import_fix import reify_concurrent_futures

reify_concurrent_futures()

from core.config_model import ConfigModel, validate_and_fix  # noqa: E402
from core.config_service import ConfigService  # noqa: E402
from core.exceptions import AppError  # noqa: E402
from core.exec_path import child_env, find_system_tool, validate_executable, validate_trusted_file  # noqa: E402
from core.secret_store import FileSecretStore  # noqa: E402
from core.service_paths import ServicePaths  # noqa: E402
from core.service_status import SYSTEMD_UNIT, WINDOWS_SERVICE_NAME, service_running  # noqa: E402
from core.version import APP_NAME, APP_VERSION  # noqa: E402

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_ADMIN = 3

#: 設定するときに場所を確かめる項目（実行ファイルは名前も確かめる）
_EXECUTABLE_FIELDS = {"update.freshclam_path": ("freshclam",), "realtime.clamonacc_path": ("clamonacc",)}
_TRUSTED_FILE_FIELDS = ("realtime.clamonacc_config_file",)


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
    print(f"内部 API: 127.0.0.1:{config.api.port}", file=out)
    if running:
        print(f"  応答: {_api_version(config.api.port) or '応答がありません'}", file=out)
    from core.clamav_service import ClamAVService

    try:
        reachable = ClamAVService(config.clamd).ping(retry=False)
    except Exception:  # noqa: BLE001 - 応答しない clamd は「停止」
        reachable = False
    print(f"clamd: {'応答あり' if reachable else '応答なし'}", file=out)
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
    config = _config_service(paths).load()
    print(json.dumps(config.to_dict(), ensure_ascii=False, indent=2), file=out)
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
    if section == "schema_version" or target is None or name not in {f.name for f in fields(target)}:
        raise CtlError(f"設定の項目が見つかりません: {key}")
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
    result = subprocess.run(command, capture_output=True, timeout=120, env=child_env(None, system))
    if result.returncode != 0:
        if action == "stop":
            raise CtlError(f"サービスを止められませんでした（{result.returncode}）")
        where = "イベントビューアーの Windows ログ（アプリケーション）" if system == "Windows" else f"journalctl -u {SYSTEMD_UNIT}"
        raise CtlError(f"サービスを開始できませんでした（{result.returncode}）。サービスは止まっています。"
                       f"原因は {where} とサービスのログで確かめてください")
    if system == "Windows" and action == "stop":
        # sc stop は止まるのを待たずに戻るため、止まるまで待つ
        import time

        for _ in range(60):
            if not service_running(system):
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
    db = DatabaseService(paths.db_file)
    db.connect()
    try:
        service = QuarantineService(db, paths.quarantine_dir)
        row = service.get(quarantine_id)
        if row.restored or row.deleted:
            raise CtlError("既に復元または削除されています")
        service.restore(quarantine_id, dest_path=dest, overwrite=overwrite)
    except AppError as exc:
        raise CtlError(f"{exc.message}（{exc.detail or exc.code}）") from None
    finally:
        db.close()
    _audit(paths, f"clamqtpie-ctl: 隔離ファイルを元の場所以外へ復元しました: id={quarantine_id} → {dest}")
    print(f"復元しました: {dest}", file=out)
    return EXIT_OK


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
    return parser


def main(argv: list[str] | None = None, out=sys.stdout, ask=input) -> int:
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
    except CtlError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
