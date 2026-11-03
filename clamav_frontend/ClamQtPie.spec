# -*- mode: python ; coding: utf-8 -*-
"""
ClamQtPie.spec

参照: インストーラ/パッケージング設計書3章、サービス化・Web API設計書 11章（段階 12-4d）
- --onedir方式（起動速度優先、DBマイグレーション設計書のAlembicスクリプト同梱のため）
- 1 つの spec で次の実行ファイルを作り、_internal/ を共有する（1 つの COLLECT）
  | 実行ファイル（Windows / Linux） | 入口 | 画面 | 管理者の権限 |
  |---|---|---|---|
  | ClamQtPieService.exe / clamqtpie-service | service_main.py | なし | SYSTEM・root のサービスとして動く（UAC なし） |
  | ClamQtPie.exe / clamqtpie | tray_main.py（トレイGUI） | あり | **求めない**（一般の利用者の権限で動く） |
  | ClamQtPieElevate.exe / clamqtpie-elevate | elevate_main.py（昇格ヘルパー） | 確認画面 | 求める（Windows は uac_admin、Linux は pkexec） |
  | clamqtpie-ctl.exe / clamqtpie-ctl | clamqtpie_ctl.py（管理用コマンド） | コンソール | 管理者・root で実行する |
  | ClamQtPieActivate.exe（Windows だけ） | notify_activate.py（通知のクリック） | なし | 求めない |
- 従来のアプリ（app.py、1 プロセス）は段階 12-4e で削除した
- UPX不使用（ウイルス対策ソフトの誤検知リスク回避、8章）
"""
import sys
from pathlib import Path

block_cipher = None

_ROOT = Path(SPECPATH)
_IS_WINDOWS = sys.platform == "win32"
_ICON = str(_ROOT / "icons" / "clamQtPie.ico") if (_ROOT / "icons" / "clamQtPie.ico").exists() else None
_VERSION = str(_ROOT / "version_info.txt") if (_ROOT / "version_info.txt").exists() else None

#: 実行ファイルの名前（Windows / Linux）。api_client/elevation.py・core/activation.py・インストーラと合わせる
_NAMES = {
    "service": "ClamQtPieService" if _IS_WINDOWS else "clamqtpie-service",
    "tray": "ClamQtPie" if _IS_WINDOWS else "clamqtpie",
    "elevate": "ClamQtPieElevate" if _IS_WINDOWS else "clamqtpie-elevate",
    "ctl": "clamqtpie-ctl",
}

_DATAS = [
    (str(_ROOT / "alembic"), "alembic"),
    (str(_ROOT / "alembic.ini"), "."),
    (str(_ROOT / "icons"), "icons"),
    (str(_ROOT / "manual"), "manual"),
    # WebUI の画面（段階 12-5b。サービスが WebUI の待ち受けで配る）
    (str(_ROOT / "web"), "web"),
]

_HIDDEN = [
    # APScheduler/Alembic系はimportlib経由の遅延ロードが多く、
    # PyInstallerの静的解析だけでは検出できないため明示指定する
    # （インストーラ/パッケージング設計書3.1節）
    "apscheduler.triggers.cron",
    "apscheduler.triggers.interval",
    "apscheduler.executors.pool",
    "apscheduler.jobstores.memory",
    "alembic.op",
    "alembic.context",
    # alembic/env.pyが実行時にimportする標準ライブラリのサブモジュール。
    # PyInstallerのフリーズ環境では自動検出されないため明示指定が必要
    # （ビルド検証時に ModuleNotFoundError: No module named 'logging.config' を確認）
    "logging.config",
] + (["win32timezone"] if _IS_WINDOWS else [])

#: サービスの内部 API（uvicorn は文字列で指定したモジュールを読み込むため、静的解析では見つからない）
_SERVICE_HIDDEN = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
] + (["win32serviceutil", "servicemanager"] if _IS_WINDOWS else [])

# トレイGUI は QtNetwork を使う（内部 API・イベントの受信。段階 12-4a）。ほかの Qt の部品は使わない
_EXCLUDES = [
    "PySide6.QtOpenGL",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtPdf",
    "PySide6.QtSvg",
    "PySide6.QtVirtualKeyboard",
]


def _analysis(script: str, hidden=(), datas=()):
    return Analysis(
        [str(_ROOT / script)],
        pathex=[str(_ROOT)],
        binaries=[],
        datas=list(datas),
        hiddenimports=_HIDDEN + list(hidden),
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=_EXCLUDES,
        win_no_prefer_redirects=False,
        win_private_assemblies=False,
        cipher=block_cipher,
        noarchive=False,
    )


# -- 不要なQt部品の除外（Phase 11の軽量化） -----------------------------------------
# PySide6のフックはQtプラグインを一式同梱するため、プラグイン経由でQuick/Qml/VirtualKeyboard
# （qtvirtualkeyboardplugin）、Pdf（qpdf）、Svg（qsvg）、TLS（tls等）といった
# 未使用のQtモジュールまで引き込まれる。DLLのimportを確認したうえで、必要なものだけ残す。
#   必要: Qt6Core/Qt6Gui/Qt6Widgets/Qt6Network + plugins/platforms/qwindows（ウィンドウ表示）、
#         plugins/styles/qmodernwindowsstyle（Windows 11スタイル）、plugins/imageformats/qico（.ico読込）
#   Qt6Network はトレイGUI の内部 API の通信に使う（127.0.0.1 の HTTP だけ。TLS のプラグインは入れない）
#   注意: Python側のSSL（SMTP送信）はlibssl-3.dll/libcrypto-3.dllを使う。
#         -x64付きの方はQtのTLSプラグイン用の別コピーで、除外してよい
_KEEP_QT_PLUGINS = {
    "platforms/qwindows.dll",
    "platforms/libqxcb.so",  # Linuxビルド用
    "platforms/libqwayland.so",
    # Waylandではシェル統合（xdg-shell）が無いとウィンドウを作れず起動できない（v0.9.0のビルドで発覚）
    "wayland-shell-integration/libxdg-shell.so",
    "wayland-shell-integration/libwl-shell-plugin.so",
    # クライアント側のウィンドウ枠（GNOMEなどサーバー側で枠を描かない環境用。libadwaitaはQtSvgが要るため除く）
    "wayland-decoration-client/libbradient.so",
    # 日本語入力（IBus）とデッドキーの入力。virtualkeyboardはQuick/Qmlが要るため除く
    "platforminputcontexts/libibusplatforminputcontextplugin.so",
    "platforminputcontexts/libcomposeplatforminputcontextplugin.so",
    "styles/qmodernwindowsstyle.dll",
    "imageformats/qico.dll",
    "imageformats/libqico.so",
}
_DROP_NAME_PREFIXES = (
    "Qt6OpenGL", "Qt6Pdf", "Qt6Qml", "Qt6Quick", "Qt6Svg", "Qt6VirtualKeyboard",
    "libQt6OpenGL", "libQt6Pdf", "libQt6Qml", "libQt6Quick", "libQt6Svg",
    "libQt6VirtualKeyboard",
    "opengl32sw",  # ソフトウェアOpenGL（Widgetsアプリでは不要、19.7MB）
    "libcrypto-3-x64", "libssl-3-x64",
)


def _keep(dest: str) -> bool:
    path = dest.replace("\\", "/")
    name = path.rsplit("/", 1)[-1]
    if "/translations/" in path or path.endswith("/translations"):
        return False  # Qtの翻訳ファイルは読み込んでいない（QTranslator未使用）
    if "/plugins/" in path:
        return path.split("/plugins/", 1)[1] in _KEEP_QT_PLUGINS
    return not name.startswith(_DROP_NAME_PREFIXES)


def _exe(analysis, name: str, console: bool, uac_admin: bool):
    analysis.binaries = [b for b in analysis.binaries if _keep(b[0])]
    analysis.datas = [d for d in analysis.datas if _keep(d[0])]
    return EXE(
        PYZ(analysis.pure, analysis.zipped_data, cipher=block_cipher),
        analysis.scripts,
        [],
        exclude_binaries=True,
        name=name,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,  # 8章: UPX圧縮はウイルス対策ソフトの誤検知リスクがあるため不使用
        console=console,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=_ICON,
        uac_admin=uac_admin,
        version=_VERSION,
    )


_parts = []
for script, key, console, uac_admin, hidden, datas in (
    ("service_main.py", "service", False, False, _SERVICE_HIDDEN, _DATAS),
    ("tray_main.py", "tray", False, False, (), _DATAS),
    # 昇格ヘルパー: Windows は requireAdministrator のマニフェストを埋め込み、起動のたびに UAC で確かめる
    ("elevate_main.py", "elevate", False, _IS_WINDOWS, (), ()),
    ("clamqtpie_ctl.py", "ctl", True, False, (), _DATAS),
):
    a = _analysis(script, hidden, datas)
    _parts += [_exe(a, _NAMES[key], console, uac_admin), a.binaries, a.zipfiles, a.datas]

# -- 通知クリック用の補助プログラム（Windowsのみ、core/activation.py） ------------------------
# 通知のクリックでWindowsから起動され、依頼ファイルを書いて終了する。ウィンドウを持たず（console=False）、
# 管理者権限も要求しない。標準ライブラリのみ使い、_internal/のPython DLLを共有する（増加は数MB）。
# Rust 化の方針により、Python のまま残す
if _IS_WINDOWS:
    helper_a = Analysis(
        [str(_ROOT / "notify_activate.py")],
        pathex=[str(_ROOT)],
        excludes=["PySide6", "shiboken6", "sqlalchemy", "alembic", "apscheduler", "watchdog", "keyring"],
        cipher=block_cipher,
        noarchive=False,
    )
    helper_exe = EXE(
        PYZ(helper_a.pure, helper_a.zipped_data, cipher=block_cipher),
        helper_a.scripts,
        [],
        exclude_binaries=True,
        name="ClamQtPieActivate",
        debug=False,
        strip=False,
        upx=False,
        console=False,
        uac_admin=False,
        icon=_ICON,
        version=_VERSION,
    )
    _parts += [helper_exe, helper_a.binaries, helper_a.zipfiles, helper_a.datas]

coll = COLLECT(
    *_parts,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ClamQtPie",
)
