"""配布物の作成（build_release.py）のテスト（段階 12-4d）。PyInstaller・ISCC は動かさない"""
from __future__ import annotations

import tarfile

import build_release


def test_linux_tarball_layout_and_owner(tmp_path):
    app = tmp_path / "dist" / "ClamQtPie"
    (app / "_internal").mkdir(parents=True)
    for name in ("clamqtpie", "clamqtpie-service", "clamqtpie-elevate", "clamqtpie-ctl", "README.md"):
        (app / name).write_bytes(b"x")
    (app / "_internal" / "lib.so").write_bytes(b"x")

    path = build_release.linux_tarball(app, "9.9.9")
    with tarfile.open(path) as tar:
        members = {m.name: m for m in tar.getmembers()}
    top = "ClamQtPie-9.9.9-linux64"
    assert {f"{top}/install.sh", f"{top}/uninstall.sh", f"{top}/share/clamqtpie.service",
            f"{top}/share/io.github.babaken.clamqtpie.policy", f"{top}/app/clamqtpie-service",
            f"{top}/app/_internal/lib.so"} <= set(members)
    # 展開した利用者の持ち物にならず、グループ・その他は書き込めない（サービスは root で動く）
    assert all(m.uid == 0 and m.gid == 0 and not m.mode & 0o022 for m in members.values())
    assert members[f"{top}/app/clamqtpie-elevate"].mode & 0o111 and members[f"{top}/install.sh"].mode == 0o755
    assert not members[f"{top}/app/README.md"].mode & 0o111


def test_sha256_file(tmp_path):
    target = tmp_path / "a.bin"
    target.write_bytes(b"abc")
    sha = build_release.write_sha256(target)
    assert sha.read_text(encoding="ascii") == \
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad  a.bin\n"


def test_iscc_is_found_in_the_usual_places(tmp_path):
    iscc = tmp_path / "Programs" / "Inno Setup 6" / "ISCC.exe"
    iscc.parent.mkdir(parents=True)
    iscc.write_bytes(b"")
    assert build_release.find_iscc({"LOCALAPPDATA": str(tmp_path)}) == iscc
    assert build_release.find_iscc({"CLAMQTPIE_ISCC": str(iscc)}) == iscc
    assert build_release.find_iscc({}) is None
