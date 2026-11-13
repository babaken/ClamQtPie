"""
build_release.py — PyInstallerビルド＋配布物の作成

参照: インストーラ/パッケージング設計書2章、サービス化・Web API設計書 11章（段階 12-4d）

使い方:
    python build_release.py [--version 0.10.1] [--no-installer]

実行後、dist/ に次のものができる（それぞれ SHA-256 の .sha256 を添える）。
- Windows: ClamQtPie-<version>-setup.exe（Inno Setup のインストーラ。packaging/windows/ClamQtPie.iss）。
  ISCC.exe は CLAMQTPIE_ISCC、%LOCALAPPDATA%\\Programs\\Inno Setup 6、%ProgramFiles(x86)%\\Inno Setup 6 の順に探す。
  --no-installer なら dist/ClamQtPie（PyInstaller の出力）までで止める
- Linux: ClamQtPie-<version>-linux64.tar.gz（app/・share/・install.sh・uninstall.sh。packaging/linux/）
--versionの既定値はcore/version.pyのAPP_VERSION（画面・診断レポートに表示する版）。
異なる版を指定した場合は、表示と配布物の名前がずれないようビルドを中止する（先にAPP_VERSIONを更新する）。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

from core.version import APP_VERSION  # noqa: E402

_PACKAGING = _ROOT / "packaging"
#: 同梱する文書（リポジトリ直下。インストーラ/パッケージング設計書2.1節）
_DOCUMENTS = ("README.md", "README_en.md", "LICENSE")
#: Linux の配布物で share/ に入れるもの（install.sh が置く）
_LINUX_SHARE = ("clamqtpie.service", "clamqtpie.desktop", "io.github.babaken.clamqtpie.policy")
#: Linux で実行の権限を付けるもの（app/ の中）
_LINUX_EXECUTABLES = ("clamqtpie", "clamqtpie-service", "clamqtpie-elevate", "clamqtpie-ctl")


def _platform_tag() -> str:
    system = platform.system()
    if system == "Windows":
        return "win64"
    if system == "Linux":
        return "linux64"
    if system == "Darwin":
        return "mac64"
    return system.lower()


def pyinstaller_build() -> Path:
    build_dir = _ROOT / "build"
    dist_dir = _ROOT / "dist"
    for d in (build_dir, dist_dir):
        if d.exists():
            shutil.rmtree(d)

    print("== PyInstallerビルド実行 ==")
    subprocess.run([sys.executable, "-m", "PyInstaller", "ClamQtPie.spec", "--noconfirm"], cwd=_ROOT, check=True)

    app_dir = dist_dir / "ClamQtPie"
    if not app_dir.exists():
        raise SystemExit("ビルド成果物 dist/ClamQtPie が見つかりません")
    for name in _DOCUMENTS:
        src = _ROOT.parent / name
        if not src.exists():
            raise SystemExit(f"同梱する文書が見つかりません: {src}")
        shutil.copy(src, app_dir / name)
    return app_dir


def find_iscc(environ=os.environ) -> Path | None:
    candidates = [environ.get("CLAMQTPIE_ISCC", "")]
    if environ.get("LOCALAPPDATA"):
        candidates.append(str(Path(environ["LOCALAPPDATA"]) / "Programs" / "Inno Setup 6" / "ISCC.exe"))
    if environ.get("ProgramFiles(x86)"):
        candidates.append(str(Path(environ["ProgramFiles(x86)"]) / "Inno Setup 6" / "ISCC.exe"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def windows_installer(app_dir: Path, version: str) -> Path:
    iscc = find_iscc()
    if iscc is None:
        raise SystemExit("Inno Setup の ISCC.exe が見つかりません（winget install JRSoftware.InnoSetup。"
                         "別の場所なら CLAMQTPIE_ISCC に指定）")
    out_dir = app_dir.parent
    print(f"== インストーラ作成（{iscc}） ==")
    subprocess.run([str(iscc), f"/DAppVersion={version}", f"/DSourceDir={app_dir}", f"/O{out_dir}",
                    str(_PACKAGING / "windows" / "ClamQtPie.iss")], check=True)
    installer = out_dir / f"ClamQtPie-{version}-setup.exe"
    if not installer.exists():
        raise SystemExit(f"インストーラが見つかりません: {installer}")
    return installer


def linux_tarball(app_dir: Path, version: str) -> Path:
    top = f"ClamQtPie-{version}-linux64"
    tar_path = app_dir.parent / f"{top}.tar.gz"
    linux = _PACKAGING / "linux"

    def owned_by_root(info: tarfile.TarInfo) -> tarfile.TarInfo:
        # 展開した人の持ち物にならないよう、root の持ち物として入れる（install.sh も chown し直す）
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        info.mode &= ~0o022  # グループ・その他の書き込みを外す
        return info

    print(f"== tar.gz 作成: {tar_path.name} ==")
    with tarfile.open(tar_path, "w:gz") as tar:
        for name in ("install.sh", "uninstall.sh"):
            info = tar.gettarinfo(str(linux / name), f"{top}/{name}")
            info.mode = 0o755
            with open(linux / name, "rb") as f:
                tar.addfile(owned_by_root(info), f)
        for name in _LINUX_SHARE:
            tar.add(str(linux / name), f"{top}/share/{name}", filter=owned_by_root)
        for path in sorted(app_dir.rglob("*")):
            arcname = f"{top}/app/{path.relative_to(app_dir).as_posix()}"
            info = tar.gettarinfo(str(path), arcname)
            if path.relative_to(app_dir).as_posix() in _LINUX_EXECUTABLES:
                info.mode |= 0o755
            if path.is_file():
                with open(path, "rb") as f:
                    tar.addfile(owned_by_root(info), f)
            else:
                tar.addfile(owned_by_root(info))
    return tar_path


def write_sha256(path: Path) -> Path:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    sha_path = path.with_name(path.name + ".sha256")
    sha_path.write_text(f"{digest.hexdigest()}  {path.name}\n", encoding="ascii")
    return sha_path


def build(version: str, installer: bool = True) -> Path:
    app_dir = pyinstaller_build()
    if platform.system() == "Windows":
        if not installer:
            print(f"== 完了（インストーラは作りません）: {app_dir} ==")
            return app_dir
        result = windows_installer(app_dir, version)
    elif platform.system() == "Linux":
        result = linux_tarball(app_dir, version)
    else:
        raise SystemExit(f"{platform.system()} の配布物は作れません")
    sha_path = write_sha256(result)
    print(f"== 完了: {result} ({result.stat().st_size / (1024 * 1024):.1f} MB) ==")
    print(sha_path.read_text(encoding="ascii").strip())
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default=APP_VERSION)
    parser.add_argument("--no-installer", action="store_true", help="Windows: インストーラを作らない")
    args = parser.parse_args()
    if args.version != APP_VERSION:
        raise SystemExit(
            f"--version {args.version} が core/version.py の APP_VERSION（{APP_VERSION}）と一致しません。"
            "先に APP_VERSION を更新してください"
        )
    build(args.version, installer=not args.no_installer)


if __name__ == "__main__":
    main()
