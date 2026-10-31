"""
テスト仕様書3.6節: NT-U03〜U05 に対応（subprocess/smtplibはモック化）
"""
import json
from unittest.mock import MagicMock, patch

from core.config_service import ConfigService
from core.exec_path import system_tool
from core.notification_service import NotificationService


def _service_for(tmp_path, **notif_overrides) -> tuple[NotificationService, ConfigService]:
    config_service = ConfigService(tmp_path / "config.json")
    model = config_service.load()
    for key, value in notif_overrides.items():
        setattr(model.notification, key, value)
    config_service.save(model)

    service = NotificationService(config_service)
    return service, config_service


def test_mail_body_template_reflects_detection_count(tmp_path):
    # NT-U03
    service, config_service = _service_for(
        tmp_path, aggregation_window_sec=0.1, max_batch_size=50
    )
    model = config_service.current
    model.smtp.host = "smtp.example.com"
    model.smtp.from_address = "from@example.com"
    model.smtp.to_address = "to@example.com"
    config_service.save(model)

    sent_bodies = []

    class _FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def starttls(self, *a, **k):
            pass

        def login(self, *a, **k):
            pass

        def sendmail(self, from_addr, to_addrs, msg):
            sent_bodies.append(msg)

        def quit(self):
            pass

    with patch("smtplib.SMTP", _FakeSMTP), patch.object(
        config_service, "get_smtp_password", return_value=None
    ), patch("subprocess.run"):
        service.notify_detection("/tmp/eicar.txt", "Eicar-Test-Signature", quarantined=True)
        import time

        time.sleep(0.3)

    assert len(sent_bodies) == 1
    import email

    msg = email.message_from_string(sent_bodies[0])
    decoded_body = msg.get_payload(decode=True).decode("utf-8")
    subject = str(email.header.make_header(email.header.decode_header(msg["Subject"])))

    assert "ウイルスを1件検知しました" in subject
    assert "/tmp/eicar.txt" in decoded_body
    assert "Eicar-Test-Signature" in decoded_body
    assert "自動的に隔離されました" in decoded_body


def test_quarantine_status_summary_uses_actual_results():
    # レビュー指摘: 以前は設定値と最後の1件で決めていたため、隔離に失敗しても「隔離されました」と送っていた
    from core.detection_aggregator import DetectionEvent
    from core.notification_service import _quarantine_status_summary

    def ev(q):
        return DetectionEvent(path="/x", virus_name="V", detected_at="t", quarantined=q)

    assert _quarantine_status_summary([ev(True), ev(True)]) == "該当ファイルは自動的に隔離されました"
    assert "対応は行われていません" in _quarantine_status_summary([ev(False)])
    mixed = _quarantine_status_summary([ev(True), ev(False), ev(False)])
    assert "1件は自動的に隔離されました" in mixed and "残りの2件は隔離されていません" in mixed


def test_mail_skipped_when_smtp_unconfigured(tmp_path):
    # NT-U04
    service, config_service = _service_for(tmp_path, aggregation_window_sec=0.1)

    # subprocess.runもモック化する（実際のToast/notify-sendを出さず、待ち時間内に処理を終えるため）
    with patch("smtplib.SMTP") as mock_smtp, patch("subprocess.run"):
        service.notify_detection("/tmp/eicar.txt", "Eicar-Test-Signature", quarantined=False)
        import time

        time.sleep(0.3)

    mock_smtp.assert_not_called()


def test_mail_send_failure_does_not_raise_and_is_not_retried(tmp_path):
    # NT-U05
    service, config_service = _service_for(tmp_path, aggregation_window_sec=0.1)
    model = config_service.current
    model.smtp.host = "smtp.example.com"
    model.smtp.from_address = "from@example.com"
    model.smtp.to_address = "to@example.com"
    config_service.save(model)

    call_count = {"n": 0}

    class _FailingSMTP:
        def __init__(self, *a, **k):
            call_count["n"] += 1
            raise ConnectionRefusedError("smtp down")

    # subprocess.runもモック化する。実際のPowerShell Toastは約1秒かかり、
    # 待ち時間内にメール送信まで到達せずテストが不安定になる（Windows実機テストで発見）
    with patch("smtplib.SMTP", _FailingSMTP), patch.object(
        config_service, "get_smtp_password", return_value=None
    ), patch("subprocess.run"):
        # 例外を送出せず処理が完了することを確認
        service.notify_detection("/tmp/eicar.txt", "Eicar-Test-Signature", quarantined=False)
        import time

        time.sleep(0.3)

    assert call_count["n"] == 1  # リトライされていない


def test_desktop_notification_uses_notify_send_on_linux(tmp_path):
    service, config_service = _service_for(tmp_path, aggregation_window_sec=0.1)

    with patch("core.notification_service.platform.system", return_value="Linux"), patch(
        "subprocess.run"
    ) as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        service.notify_detection("/tmp/eicar.txt", "Eicar-Test-Signature", quarantined=True)
        import time

        time.sleep(0.3)

    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert args[0] == system_tool("notify-send", "Linux")
    assert "ウイルスを検知しました" in args[1]


def test_desktop_notification_uses_powershell_toast_on_windows(tmp_path):
    service, config_service = _service_for(tmp_path, aggregation_window_sec=0.1)

    with patch("core.notification_service.platform.system", return_value="Windows"), patch(
        "subprocess.run"
    ) as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        service.notify_detection("C:\\eicar.txt", "Eicar-Test-Signature", quarantined=True)
        import time

        time.sleep(0.3)

    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert args[0] == system_tool("powershell", "Windows")
    assert "ToastNotificationManager" in args[-1]
    # 検知内容はスクリプトに埋め込まず、環境変数で渡す（コード注入の防止）
    assert "ウイルスを検知しました" not in args[-1]
    assert "$env:CLAMQTPIE_TOAST_TITLE" in args[-1]
    env = mock_run.call_args.kwargs["env"]
    assert env["CLAMQTPIE_TOAST_TITLE"] == "ウイルスを検知しました"
    assert "C:\\eicar.txt" in env["CLAMQTPIE_TOAST_BODY"]


def test_update_failure_notification_sent_when_enabled(tmp_path):
    service, config_service = _service_for(tmp_path, on_update_fail=True)

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        service.notify_update_failure("接続エラー")

    mock_run.assert_called_once()


def test_update_failure_notification_skipped_when_disabled(tmp_path):
    service, config_service = _service_for(tmp_path, on_update_fail=False)

    with patch("subprocess.run") as mock_run:
        service.notify_update_failure("接続エラー")

    mock_run.assert_not_called()


def test_detection_toast_opens_quarantine_on_click_but_failure_toast_does_not(tmp_path):
    service, _ = _service_for(tmp_path, aggregation_window_sec=0.1)

    with patch("core.notification_service.platform.system", return_value="Windows"), patch(
        "subprocess.run"
    ) as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        service.notify_detection(r"C:\eicar.txt", "Eicar-Test-Signature", quarantined=True)
        import time

        time.sleep(0.3)
        service.notify_update_failure("接続エラー")

    detection_script, failure_script = (c.args[0][-1] for c in mock_run.call_args_list)
    assert "SetAttribute('launch', 'clamqtpie:quarantine')" in detection_script
    assert "activationType" not in failure_script


def _smtp_config(config_service, encryption, host="127.0.0.1", port=25):
    model = config_service.current
    model.smtp.host = host
    model.smtp.port = port
    model.smtp.from_address = "from@example.com"
    model.smtp.to_address = "to@example.com"
    model.smtp.user = "user@example.com"
    model.smtp.encryption = encryption
    config_service.save(model)


def test_smtp_ssl_passes_verifying_context(tmp_path):
    # セキュリティレビュー: SMTP_SSLにcontextを渡さないと証明書を検証せず、MITMで資格情報が漏れる
    import ssl

    service, config_service = _service_for(tmp_path)
    _smtp_config(config_service, "ssl", port=465)
    captured = {}

    class _FakeSMTPSSL:
        def __init__(self, *a, **k):
            captured["context"] = k.get("context")

        def login(self, *a):
            pass

        def sendmail(self, *a):
            pass

        def quit(self):
            pass

    with patch("smtplib.SMTP_SSL", _FakeSMTPSSL):
        service._smtp_send(config_service.current.smtp, "pw", "s", "b")

    ctx = captured["context"]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_smtp_starttls_passes_verifying_context(tmp_path):
    import ssl

    service, config_service = _service_for(tmp_path)
    _smtp_config(config_service, "starttls", port=587)
    captured = {}

    class _FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def starttls(self, *a, **k):
            captured["context"] = k.get("context")

        def login(self, *a):
            pass

        def sendmail(self, *a):
            pass

        def quit(self):
            pass

    with patch("smtplib.SMTP", _FakeSMTP):
        service._smtp_send(config_service.current.smtp, "pw", "s", "b")

    ctx = captured["context"]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_smtp_ssl_rejects_untrusted_certificate(tmp_path):
    # 実際に自己署名・CN不一致のTLSサーバを立て、接続が証明書エラーで拒否されることを確認する
    import socket
    import ssl
    import subprocess
    import tempfile
    import threading

    import pytest

    d = tempfile.mkdtemp()
    cert, key = f"{d}/c.pem", f"{d}/k.pem"
    try:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", key, "-out", cert,
             "-days", "1", "-nodes", "-subj", "/CN=wrong-host.invalid"],
            check=True, capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("opensslが無いため自己署名証明書を用意できない")

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    def _serve():
        try:
            ctx.wrap_socket(srv.accept()[0], server_side=True)
        except OSError:
            pass  # クライアントが証明書エラーで握手を中断する（想定どおり）

    threading.Thread(target=_serve, daemon=True).start()

    service, config_service = _service_for(tmp_path)
    _smtp_config(config_service, "ssl", port=port)

    with pytest.raises(ssl.SSLCertVerificationError):
        service._smtp_send(config_service.current.smtp, "pw", "s", "b")


def test_toast_does_not_embed_detection_content_in_script(tmp_path):
    # セキュリティレビュー: 検知ファイル名のUnicode引用符（’‘）でPowerShellのエスケープを回避し
    # 任意コードを実行できた。検知内容をスクリプト文字列に埋め込まず環境変数で渡すことで防ぐ
    service, _ = _service_for(tmp_path, aggregation_window_sec=0.1)
    evil = "invoice’)); Set-Content -Path 'C:\\pwned.txt' -Value PWNED; ((’.pdf"

    with patch("core.notification_service.platform.system", return_value="Windows"), patch(
        "subprocess.run"
    ) as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        service.notify_detection(f"C:\\Users\\u\\Downloads\\{evil}", "Eicar-Test-Signature", quarantined=True)
        import time

        time.sleep(0.3)

    script = mock_run.call_args[0][0][-1]
    env = mock_run.call_args.kwargs["env"]
    # 悪意ある文字列はスクリプトに一切現れず、環境変数の値（=無害なテキスト）としてのみ渡る
    assert "Set-Content" not in script
    assert "’" not in script
    assert "pwned" not in script
    assert evil in env["CLAMQTPIE_TOAST_BODY"]


def test_toast_passes_title_and_body_only_via_env(tmp_path):
    service, _ = _service_for(tmp_path, aggregation_window_sec=0.1)

    with patch("core.notification_service.platform.system", return_value="Windows"), patch(
        "subprocess.run"
    ) as mock_run, patch.dict("os.environ", {"CLAMQT_TEST_INHERITED": "1"}):
        mock_run.return_value = MagicMock(returncode=0)
        service.notify_detection("C:\\a.txt", "Win.Test.EICAR", quarantined=False)
        import time

        time.sleep(0.3)

    env = mock_run.call_args.kwargs["env"]
    # 管理者権限のPowerShellには、利用者が変えられる環境変数を引き継がず、決まった変数と通知用の2つだけを渡す
    assert "CLAMQT_TEST_INHERITED" not in env
    assert set(["CLAMQTPIE_TOAST_TITLE", "CLAMQTPIE_TOAST_BODY", "SystemRoot", "PATH"]).issubset(env)


def test_notify_send_text_is_escaped_so_markup_is_shown_literally(tmp_path):
    # ファイル名に含まれる<・>・&が、通知デーモンにマークアップとして解釈されないこと
    service, _ = _service_for(tmp_path)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        service._send_linux_notify_send("検知 <b>", '/tmp/<a href="http://x">a</a>&b.txt: Eicar')
    args = mock_run.call_args[0][0]
    assert args[1] == "検知 &lt;b&gt;"
    assert args[2] == '/tmp/&lt;a href="http://x"&gt;a&lt;/a&gt;&amp;b.txt: Eicar'



def test_notify_send_shows_app_name_and_icon(tmp_path):
    # 通知の差出人がClamQtPieと分かるよう、アプリ名とアイコンを付けること
    from core.version import APP_NAME

    config_service = ConfigService(tmp_path / "config.json")
    config_service.load()
    service = NotificationService(config_service, icon_path="/opt/clamqtpie/icons/app.png")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        service._send_linux_notify_send("検知", "/tmp/a.txt: Eicar")
    args = mock_run.call_args[0][0]
    assert f"--app-name={APP_NAME}" in args
    assert "--icon=/opt/clamqtpie/icons/app.png" in args

def test_smtp_without_encryption_does_not_send_password(tmp_path):
    # 暗号化しない接続では、パスワードを平文で送らないよう認証しない（送信自体は行う）
    service, config_service = _service_for(tmp_path)
    _smtp_config(config_service, "none", port=25)
    calls = []

    class _FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def login(self, *a):
            calls.append("login")

        def sendmail(self, *a):
            calls.append("sendmail")

        def quit(self):
            pass

    with patch("smtplib.SMTP", _FakeSMTP):
        service._smtp_send(config_service.current.smtp, "pw", "s", "b")

    assert calls == ["sendmail"]
