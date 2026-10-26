"""
clamd.confの読み取り（core/clamd_conf.py）のテスト
"""
from pathlib import Path

from core.clamd_conf import find_clamd_conf, load_clamd_conf, parse_clamd_conf


def _parse(text):
    return parse_clamd_conf(text, Path("/etc/clamav/clamd.conf"))


def test_parse_ignores_comments_and_keeps_multiple_values():
    conf = _parse(
        "# コメント\n"
        "\n"
        "LocalSocket /run/clamav/clamd.ctl\n"
        "OnAccessIncludePath\t/home\n"
        "OnAccessIncludePath /srv\n"
        'TemporaryDirectory "/var/tmp"\n'
        "#OnAccessPrevention yes\n"
    )
    assert conf.get("localsocket") == "/run/clamav/clamd.ctl"
    assert conf.get_all("OnAccessIncludePath") == ["/home", "/srv"]
    assert conf.temporary_directory == "/var/tmp"
    assert conf.get("OnAccessPrevention") is None


def test_defaults_and_flags():
    conf = _parse("TCPSocket 3310\nOnAccessExcludeRootUID no\n")
    assert conf.temporary_directory == "/tmp"
    assert conf.has_local_socket is False
    assert conf.is_local_daemon is True  # TCPAddr未指定はローカル
    assert conf.has_on_access_exclude_user is False
    assert _parse("OnAccessExcludeRootUID yes\n").has_on_access_exclude_user is True


def test_remote_tcp_daemon_is_not_local():
    assert _parse("TCPSocket 3310\nTCPAddr 10.0.0.5\n").is_local_daemon is False
    assert _parse("TCPSocket 3310\nTCPAddr 127.0.0.1\n").is_local_daemon is True


def test_find_prefers_configured_file_and_falls_back_to_candidates(tmp_path):
    second = tmp_path / "scan.conf"
    second.write_text("LocalSocket /x\n", encoding="utf-8")
    candidates = (str(tmp_path / "missing.conf"), str(second))

    assert find_clamd_conf("", candidates) == second
    assert find_clamd_conf("/etc/custom.conf", candidates) == Path("/etc/custom.conf")
    assert find_clamd_conf("", (str(tmp_path / "missing.conf"),)) is None


def test_load_returns_none_when_unreadable(tmp_path):
    assert load_clamd_conf(str(tmp_path / "missing.conf")) is None
    conf_path = tmp_path / "clamd.conf"
    conf_path.write_text("OnAccessExcludeUname clamav\n", encoding="utf-8")
    assert load_clamd_conf(str(conf_path)).has_on_access_exclude_user is True
