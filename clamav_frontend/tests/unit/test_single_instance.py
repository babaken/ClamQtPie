"""
タスクトレイ常駐アプリプロセス設計書3章のテスト（Linux: flock方式 / Windows: named mutex方式）
"""
import sys
import uuid
from unittest.mock import patch

import pytest

from core.single_instance import SingleInstanceLock


@pytest.fixture(autouse=True)
def _unique_mutex_name(monkeypatch):
    # Windowsでは実際のGlobal mutexを使うため、起動中のアプリ本体と衝突しないよう
    # テストごとに一意な名前へ差し替える
    monkeypatch.setattr(
        "core.single_instance._MUTEX_NAME", f"Local\\ClamAVFrontend_Test_{uuid.uuid4().hex}"
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named mutex固有の挙動")
def test_access_denied_mutex_is_treated_as_already_running(tmp_path):
    # 管理者権限で起動中のインスタンスのmutexに通常権限からアクセスした場合
    import pywintypes
    import winerror

    error = pywintypes.error(winerror.ERROR_ACCESS_DENIED, "CreateMutex", "アクセスが拒否されました。")
    with patch("win32event.CreateMutex", side_effect=error):
        assert SingleInstanceLock(tmp_path).acquire() is False


def test_first_acquire_succeeds(tmp_path):
    lock = SingleInstanceLock(tmp_path)
    assert lock.acquire() is True
    lock.release()


def test_second_acquire_fails_while_first_holds_lock(tmp_path):
    lock1 = SingleInstanceLock(tmp_path)
    lock2 = SingleInstanceLock(tmp_path)

    assert lock1.acquire() is True
    assert lock2.acquire() is False  # 多重起動検知

    lock1.release()


def test_acquire_succeeds_again_after_release(tmp_path):
    lock1 = SingleInstanceLock(tmp_path)
    assert lock1.acquire() is True
    lock1.release()

    lock2 = SingleInstanceLock(tmp_path)
    assert lock2.acquire() is True
    lock2.release()


def test_context_manager_releases_on_exit(tmp_path):
    with SingleInstanceLock(tmp_path) as lock:
        assert lock is not None

    lock2 = SingleInstanceLock(tmp_path)
    assert lock2.acquire() is True
    lock2.release()


def test_default_lock_dir_is_per_user_data_dir(monkeypatch, tmp_path):
    # タスクトレイ常駐アプリプロセス設計書3.1節: ~/.local/share/clamqtpie/app.lock
    from core.single_instance import default_lock_dir

    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert default_lock_dir() == tmp_path / ".local" / "share" / "clamqtpie"

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert default_lock_dir() == tmp_path / "xdg" / "clamqtpie"
