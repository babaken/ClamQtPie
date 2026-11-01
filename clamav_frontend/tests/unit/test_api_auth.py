"""Web API の認証と要求の確認のテスト（サービス化・Web API設計書 3.1節・3.2節）"""
from __future__ import annotations

import os
import sys

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from api.app import API_PREFIX, create_app
from api.auth import ROLE_ADMIN, ROLE_USER, ApiTokens, require_admin, require_user


@pytest.fixture
def tokens(tmp_path):
    return ApiTokens(tmp_path / "api_tokens")


@pytest.fixture
def app(tokens):
    app = create_app(tokens)

    def user_only(role: str = Depends(require_user)):
        return {"role": role}

    def admin_only(role: str = Depends(require_admin)):
        return {"role": role}

    def change_something(role: str = Depends(require_user)):
        return {"role": role}

    app.add_api_route(f"{API_PREFIX}/_user", user_only, methods=["GET"])
    app.add_api_route(f"{API_PREFIX}/_admin", admin_only, methods=["GET"])
    app.add_api_route(f"{API_PREFIX}/_change", change_something, methods=["POST"])
    return app


def _client(app, client_host="127.0.0.1", base_url="http://127.0.0.1:8765"):
    return TestClient(app, base_url=base_url, client=(client_host, 50000))


def _bearer(tokens, role):
    return {"Authorization": f"Bearer {tokens.token_for(role)}"}


# -- トークン --------------------------------------------------------------------
def test_tokens_are_random_and_identify_their_role(tmp_path):
    first, second = ApiTokens(tmp_path), ApiTokens(tmp_path)
    assert first.token_for(ROLE_USER) != second.token_for(ROLE_USER)  # 起動のたびに作り直す
    assert first.token_for(ROLE_USER) != first.token_for(ROLE_ADMIN)
    assert len(first.token_for(ROLE_ADMIN)) >= 43  # 32 バイト
    assert first.role_of(first.token_for(ROLE_USER)) == ROLE_USER
    assert first.role_of(first.token_for(ROLE_ADMIN)) == ROLE_ADMIN
    assert first.role_of(second.token_for(ROLE_ADMIN)) is None
    assert first.role_of("") is None


def test_token_files_are_written_and_removed(tokens):
    tokens.write_files()
    for role in (ROLE_USER, ROLE_ADMIN):
        assert tokens.path_for(role).read_text(encoding="ascii") == tokens.token_for(role)
    assert list(tokens.directory.glob("*.tmp")) == []
    tokens.remove_files()
    assert list(tokens.directory.glob("*.token")) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の権限のテスト")
def test_token_files_are_readable_only_by_owner_on_posix(tokens):
    tokens.write_files()
    assert os.stat(tokens.directory).st_mode & 0o777 == 0o700
    for role in (ROLE_USER, ROLE_ADMIN):
        assert os.stat(tokens.path_for(role)).st_mode & 0o777 == 0o600


@pytest.mark.skipif(sys.platform != "win32", reason="Windows のアクセス権のテスト")
def test_token_files_are_readable_only_by_owner_system_and_admins_on_windows(tokens):
    import win32api
    import win32security

    tokens.write_files()
    process_token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    owner = win32security.ConvertSidToStringSid(
        win32security.GetTokenInformation(process_token, win32security.TokenUser)[0]
    )
    expected = {owner, "S-1-5-18", "S-1-5-32-544"}
    for path in (tokens.directory, tokens.path_for(ROLE_USER), tokens.path_for(ROLE_ADMIN)):
        descriptor = win32security.GetNamedSecurityInfo(
            str(path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION
        )
        dacl = descriptor.GetSecurityDescriptorDacl()
        sids = {win32security.ConvertSidToStringSid(dacl.GetAce(i)[2]) for i in range(dacl.GetAceCount())}
        assert sids == expected, path  # Users・Everyone などは読めない（親フォルダから引き継がない）


# -- ロール ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic dXNlcjpwYXNz"}, {"Authorization": "Bearer "}],
)
def test_missing_or_wrong_token_is_401(app, headers):
    response = _client(app).get(f"{API_PREFIX}/_user", headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "E8101"
    assert response.headers["www-authenticate"] == "Bearer"


def test_user_token_cannot_use_admin_api(app, tokens):
    response = _client(app).get(f"{API_PREFIX}/_admin", headers=_bearer(tokens, ROLE_USER))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "E8102"


def test_admin_token_can_use_user_and_admin_api(app, tokens):
    client = _client(app)
    assert client.get(f"{API_PREFIX}/_user", headers=_bearer(tokens, ROLE_ADMIN)).json() == {"role": "admin"}
    assert client.get(f"{API_PREFIX}/_admin", headers=_bearer(tokens, ROLE_ADMIN)).json() == {"role": "admin"}
    assert client.get(f"{API_PREFIX}/_user", headers=_bearer(tokens, ROLE_USER)).json() == {"role": "user"}


def test_version_needs_no_token(app):
    assert _client(app).get(f"{API_PREFIX}/version").status_code == 200


# -- 接続元・Host・Origin -----------------------------------------------------------
@pytest.mark.parametrize("client_host", ["127.0.0.1", "::1"])
def test_loopback_clients_are_accepted(app, tokens, client_host):
    assert _client(app, client_host=client_host).get(f"{API_PREFIX}/_user", headers=_bearer(tokens, ROLE_USER)).status_code == 200


def test_request_from_another_machine_is_rejected_even_with_valid_token(app, tokens):
    response = _client(app, client_host="192.168.1.20").get(f"{API_PREFIX}/_user", headers=_bearer(tokens, ROLE_ADMIN))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "E8103"


@pytest.mark.parametrize(
    "host",
    ["evil.example:8765", "127.0.0.1:9999", "127.0.0.1", "192.168.1.10:8765", "localhost.evil.example:8765"],
)
def test_wrong_host_header_is_400(app, tokens, host):
    # 別の名前・別のポートでこのPC に向けた要求（DNS リバインディング）を受け付けない
    response = _client(app).get(f"{API_PREFIX}/_user", headers={**_bearer(tokens, ROLE_ADMIN), "Host": host})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "E8103"


def test_localhost_host_header_is_accepted(app, tokens):
    client = _client(app, base_url="http://localhost:8765")
    assert client.get(f"{API_PREFIX}/_user", headers=_bearer(tokens, ROLE_USER)).status_code == 200


@pytest.mark.parametrize("header", ["Origin", "Referer"])
def test_state_change_from_foreign_page_is_rejected(app, tokens, header):
    value = "https://evil.example" if header == "Origin" else "https://evil.example/page"
    response = _client(app).post(f"{API_PREFIX}/_change", headers={**_bearer(tokens, ROLE_ADMIN), header: value})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "E8103"


@pytest.mark.parametrize("extra", [{}, {"Origin": "http://127.0.0.1:8765"}, {"Referer": "http://localhost:8765/x"}])
def test_state_change_without_origin_or_from_this_pc_is_accepted(app, tokens, extra):
    response = _client(app).post(f"{API_PREFIX}/_change", headers={**_bearer(tokens, ROLE_USER), **extra})
    assert response.status_code == 200


def test_rejected_request_does_not_reach_the_route(app, tokens):
    called = []
    app.add_api_route(f"{API_PREFIX}/_spy", lambda: called.append(1), methods=["POST"])
    _client(app).post(f"{API_PREFIX}/_spy", headers={"Origin": "https://evil.example"})
    assert called == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX のシンボリックリンクのテスト")
def test_token_dir_replaced_by_link_is_refused(tmp_path):
    # トークンのフォルダがリンクに置き換えられていたら、リンク先へ書き出さない
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o755)
    (tmp_path / "api_tokens").symlink_to(elsewhere, target_is_directory=True)
    tokens = ApiTokens(tmp_path / "api_tokens")
    with pytest.raises(OSError):
        tokens.write_files()
    assert list(elsewhere.iterdir()) == []
    assert os.stat(elsewhere).st_mode & 0o777 == 0o755
