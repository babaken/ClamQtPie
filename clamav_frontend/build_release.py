"""
build_release.py — PyInstallerビルド＋配布用Zip作成

参照: インストーラ/パッケージング設計書2章（Zipパッケージ内容）

使い方:
    python build_release.py [--version 0.9.1]

実行後、dist/ClamQtPie_v<version>_<platform>.zip が生成される。
--versionの既定値はcore/version.pyのAPP_VERSION（画面・診断レポートに表示する版）。
異なる版を指定した場合は、表示とZip名がずれないようビルドを中止する（先にAPP_VERSIONを更新する）。
"""
from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

from core.version import APP_VERSION  # noqa: E402


def _platform_tag() -> str:
    system = platform.system()
    if system == "Windows":
        return "win64"
    if system == "Linux":
        return "linux64"
    if system == "Darwin":
        return "mac64"
    return system.lower()


def build(version: str) -> Path:
    build_dir = _ROOT / "build"
    dist_dir = _ROOT / "dist"
    for d in (build_dir, dist_dir):
        if d.exists():
            shutil.rmtree(d)

    print("== PyInstallerビルド実行 ==")
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "ClamQtPie.spec", "--noconfirm"],
        cwd=_ROOT,
        check=True,
    )

    app_dir = dist_dir / "ClamQtPie"
    if not app_dir.exists():
        raise SystemExit("ビルド成果物 dist/ClamQtPie が見つかりません")

    # 配布物構成に必要なREADMEを同梱する（インストーラ/パッケージング設計書2.1節）。
    # READMEはリポジトリ直下のもの（日本語: README.md / 英語: README_en.md）に一本化している
    for readme in ("README.md", "README_en.md", "LICENSE"):
        src = _ROOT.parent / readme
        if not src.exists():
            raise SystemExit(f"同梱するREADMEが見つかりません: {src}")
        shutil.copy(src, app_dir / readme)

    zip_name = f"ClamQtPie_v{version}_{_platform_tag()}.zip"
    zip_path = dist_dir / zip_name

    print(f"== Zip作成: {zip_name} ==")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in app_dir.rglob("*"):
            if file_path.is_file():
                arcname = Path("ClamQtPie") / file_path.relative_to(app_dir)
                zf.write(file_path, arcname)

    print(f"== 完了: {zip_path} ({zip_path.stat().st_size / (1024 * 1024):.1f} MB) ==")
    return zip_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default=APP_VERSION)
    args = parser.parse_args()
    if args.version != APP_VERSION:
        raise SystemExit(
            f"--version {args.version} が core/version.py の APP_VERSION（{APP_VERSION}）と一致しません。"
            "先に APP_VERSION を更新してください"
        )
    build(args.version)


if __name__ == "__main__":
    main()
