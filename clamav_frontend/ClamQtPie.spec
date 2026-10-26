# -*- mode: python ; coding: utf-8 -*-
"""
ClamQtPie.spec

参照: インストーラ/パッケージング設計書3章
- --onedir方式（起動速度優先、DBマイグレーション設計書のAlembicスクリプト同梱のため）
- uac_admin=True（常時管理者権限起動、4章）
- UPX不使用（ウイルス対策ソフトの誤検知リスク回避、8章）
"""
import sys
from pathlib import Path

block_cipher = None

_ROOT = Path(SPECPATH)

a = Analysis(
    [str(_ROOT / "app.py")],
    pathex=[str(_ROOT)],
    binaries=[],
    datas=[
        (str(_ROOT / "alembic"), "alembic"),
        (str(_ROOT / "alembic.ini"), "."),
        (str(_ROOT / "icons"), "icons"),
        (str(_ROOT / "manual"), "manual"),
    ],
    hiddenimports=[
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
    ]
    + (["win32timezone"] if sys.platform == "win32" else []),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 本アプリはQtCore/QtGui/QtWidgetsのみ使用する（Phase 11の軽量化）
    excludes=[
        "PySide6.QtNetwork",
        "PySide6.QtOpenGL",
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtPdf",
        "PySide6.QtSvg",
        "PySide6.QtVirtualKeyboard",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)


# -- 不要なQt部品の除外（Phase 11の軽量化） -----------------------------------------
# PySide6のフックはQtプラグインを一式同梱するため、プラグイン経由でQuick/Qml/VirtualKeyboard
# （qtvirtualkeyboardplugin）、Pdf（qpdf）、Svg（qsvg）、Network+OpenSSL（tls等）といった
# 未使用のQtモジュールまで引き込まれる。DLLのimportを確認したうえで、必要なものだけ残す。
#   必要: Qt6Core/Qt6Gui/Qt6Widgets + plugins/platforms/qwindows（ウィンドウ表示）、
#         plugins/styles/qmodernwindowsstyle（Windows 11スタイル）、plugins/imageformats/qico（.ico読込）
#   注意: Python側のSSL（SMTP送信）はlibssl-3.dll/libcrypto-3.dllを使う。
#         -x64付きの方はQtのTLSプラグイン用の別コピーで、除外してよい
_KEEP_QT_PLUGINS = {
    "platforms/qwindows.dll",
    "platforms/libqxcb.so",  # Linuxビルド用
    "platforms/libqwayland.so",
    "styles/qmodernwindowsstyle.dll",
    "imageformats/qico.dll",
    "imageformats/libqico.so",
}
_DROP_NAME_PREFIXES = (
    "Qt6Network", "Qt6OpenGL", "Qt6Pdf", "Qt6Qml", "Qt6Quick", "Qt6Svg", "Qt6VirtualKeyboard",
    "libQt6Network", "libQt6OpenGL", "libQt6Pdf", "libQt6Qml", "libQt6Quick", "libQt6Svg",
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


a.binaries = [b for b in a.binaries if _keep(b[0])]
a.datas = [d for d in a.datas if _keep(d[0])]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ClamQtPie",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # 8章: UPX圧縮はウイルス対策ソフトの誤検知リスクがあるため不使用
    console=False,  # GUIアプリのためコンソール非表示
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(_ROOT / "icons" / "clamQtPie.ico") if (_ROOT / "icons" / "clamQtPie.ico").exists() else None,
    uac_admin=True,  # 4章: 常時管理者権限で起動（requireAdministratorマニフェスト埋め込み）
    version=str(_ROOT / "version_info.txt") if (_ROOT / "version_info.txt").exists() else None,
)

# -- 通知クリック用の補助プログラム（Windowsのみ、core/activation.py） ------------------------
# 通知のクリックでWindowsから起動され、依頼ファイルを書いて終了する。ウィンドウを持たず（console=False）、
# 管理者権限も要求しない（uac_admin=False。本体を直接起動するとクリックのたびにUACが出るため）。
# 標準ライブラリのみ使うため、本体と同じフォルダに置き_internal/のPython DLLを共有する（増加は数MB）
_helper_exes = []
if sys.platform == "win32":
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
        icon=str(_ROOT / "icons" / "clamQtPie.ico") if (_ROOT / "icons" / "clamQtPie.ico").exists() else None,
        version=str(_ROOT / "version_info.txt") if (_ROOT / "version_info.txt").exists() else None,
    )
    _helper_exes = [helper_exe, helper_a.binaries, helper_a.zipfiles, helper_a.datas]

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    *_helper_exes,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ClamQtPie",
)
