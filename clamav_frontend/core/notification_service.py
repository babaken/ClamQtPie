"""
NotificationService — デスクトップ通知・メール通知

参照: 通知機能詳細仕様書 全体
- デスクトップ通知: Windows PowerShell ToastNotificationManager / Linux notify-send
- メール通知: プレーンテキストテンプレート、失敗時はリトライせずログのみ
- 検知通知はDetectionAggregatorで60秒集約、それ以外（更新失敗・ジョブ失敗）は都度通知
"""
from __future__ import annotations

import html
import logging
import platform
import ssl
import smtplib
import subprocess
from email.mime.text import MIMEText

from core.activation import ALLOWED_SCREENS, SCREEN_QUARANTINE, toast_launch_uri
from core.config_model import ConfigModel
from core.config_service import ConfigService
from core.detection_aggregator import DetectionAggregator, DetectionEvent
from core.exceptions import DesktopNotifyError, KeyringUnavailableError, MailSendError
from core.exec_path import child_env, system_tool
from core.text_safety import visible_controls
from core.version import APP_ID, APP_NAME

logger = logging.getLogger("clamav_frontend")

_MAIL_TEMPLATE = """\
以下のとおりウイルスを検知しました。

検知日時（最初の検知）: {first_detected_at}
検知件数: {count}件

--------------------------------
{detections_block}
--------------------------------

対応状況: {quarantine_status_summary}

本メールは{app_name}から自動送信されています。
"""

_DETECTION_BLOCK_TEMPLATE = """\
ファイル: {path}
ウイルス名: {virus_name}
検知日時: {detected_at}
--------------------------------
"""


def _quarantine_status_summary(events: list[DetectionEvent]) -> str:
    """メールの「対応状況」。実際に隔離できた件数から作る

    以前は設定値（default_action）と最後の1件の値で決めていたため、隔離に失敗しても
    「自動的に隔離されました」と送っていた（コードレビューで発見）。
    """
    quarantined = sum(1 for e in events if e.quarantined)
    if quarantined == len(events):
        return "該当ファイルは自動的に隔離されました"
    if quarantined == 0:
        return "該当ファイルへの対応は行われていません。隔離画面から手動対応してください"
    return (
        f"{quarantined}件は自動的に隔離されました。残りの{len(events) - quarantined}件は隔離されていません"
        "（隔離に失敗したか、対応しない設定です）。ログを確認し、手動で対応してください"
    )


class NotificationService:
    def __init__(self, config_service: ConfigService, icon_path: str | None = None) -> None:
        self._config_service = config_service
        self._icon_path = icon_path
        self._aggregator: DetectionAggregator | None = None
        self._rebuild_aggregator()

    @property
    def config(self) -> ConfigModel:
        return self._config_service.current

    def _rebuild_aggregator(self) -> None:
        notif = self.config.notification
        self._aggregator = DetectionAggregator(
            window_sec=notif.aggregation_window_sec,
            max_batch_size=notif.max_batch_size,
            on_batch_ready=self._on_detection_batch_ready,
        )

    # -- 検知通知（集約あり、通知機能詳細仕様書3章） -----------------------------
    def notify_detection(self, path: str, virus_name: str, quarantined: bool) -> None:
        """quarantined: 実際に隔離できたか（設定値ではなく隔離の結果を渡す）"""
        if self._aggregator is None:
            self._rebuild_aggregator()
        from datetime import datetime, timezone

        # ファイル名・ウイルス名の制御文字（改行など）で、通知・メールの本文が別の行に分かれないようにする
        event = DetectionEvent(
            path=visible_controls(path),
            virus_name=visible_controls(virus_name),
            detected_at=datetime.now(timezone.utc).isoformat(),
            quarantined=quarantined,
        )
        self._aggregator.add(event)

    def _on_detection_batch_ready(self, events: list[DetectionEvent]) -> None:
        notif = self.config.notification
        count = len(events)
        title = "ウイルスを検知しました" if count == 1 else f"ウイルスを{count}件検知しました"

        if notif.on_detect_desktop:
            # 通知機能詳細仕様書4.1節: クリックで隔離画面を開く（core/activation.py）
            self._send_desktop_notification(title, self._desktop_body(events), open_screen=SCREEN_QUARANTINE)

        if notif.on_detect_mail:
            self._send_detection_mail(events)

    def _desktop_body(self, events: list[DetectionEvent]) -> str:
        if len(events) == 1:
            e = events[0]
            return f"{e.path}: {e.virus_name}"
        head = events[:2]
        body = "\n".join(f"{e.path}: {e.virus_name}" for e in head)
        remaining = len(events) - 2
        if remaining > 0:
            body += f"\n他{remaining}件"
        return body

    # -- 定義更新失敗・ジョブ失敗（集約なし、通知機能詳細仕様書4.3節） -----------------
    def notify_update_failure(self, detail: str) -> None:
        if not self.config.notification.on_update_fail:
            return
        self._send_desktop_notification("定義データベースの更新に失敗しました", detail)

    def notify_definitions_stale(self, age_days: int) -> None:
        """定義データベースが古くなった（update.stale_daysを超えた）ことを知らせる。定義更新の失敗と同じ設定に従う"""
        if not self.config.notification.on_update_fail:
            return
        self._send_desktop_notification(
            "ウイルス定義が古くなっています",
            f"定義データベースが {age_days} 日更新されていません。定義更新を実行してください",
        )

    def notify_job_failure(self, job_name: str, detail: str) -> None:
        if not self.config.notification.on_job_fail:
            return
        self._send_desktop_notification(
            "スケジュールジョブが失敗しました", f"{job_name}: {detail}"
        )

    # -- デスクトップ通知（4章） -----------------------------------------------
    def _send_desktop_notification(self, title: str, body: str, open_screen: str | None = None) -> None:
        """open_screen: クリック時に開く画面（Windows Toastのみ。Linuxのnotify-sendは仕様書4.2節どおりクリック動作なし）"""
        try:
            if platform.system() == "Windows":
                self._send_windows_toast(title, body, open_screen)
            else:
                self._send_linux_notify_send(title, body)
        except DesktopNotifyError as exc:
            logger.warning(
                f"デスクトップ通知の表示に失敗しました (detail: {exc.detail})",
                extra={"error_code": exc.code},
            )

    def _send_windows_toast(self, title: str, body: str, open_screen: str | None = None) -> None:
        # PowerShell ToastNotificationManager経由（通知機能詳細仕様書4.1節）。
        # 検知内容（title/body）はPowerShellスクリプトに文字列として埋め込まず、環境変数で渡して
        # $env:経由で読む。環境変数の値はコードとして解釈されないため、ファイル名にどんな文字が含まれていても、
        # 通知の文字列としてだけ扱われる
        launch = ""
        if open_screen in ALLOWED_SCREENS:
            # クリックで clamqtpie:<画面> を開く（固定値のみ。toast_launch_uriの戻り値はALLOWED_SCREENS由来で安全）
            launch = (
                "$template.DocumentElement.SetAttribute('activationType', 'protocol'); "
                f"$template.DocumentElement.SetAttribute('launch', '{toast_launch_uri(open_screen)}'); "
            )
        script = (
            "[Windows.UI.Notifications.ToastNotificationManager, "
            "Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null; "
            "$template = [Windows.UI.Notifications.ToastNotificationManager]::"
            "GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
            "$texts = $template.GetElementsByTagName('text'); "
            "$texts.Item(0).AppendChild($template.CreateTextNode($env:CLAMQTPIE_TOAST_TITLE)) | Out-Null; "
            "$texts.Item(1).AppendChild($template.CreateTextNode($env:CLAMQTPIE_TOAST_BODY)) | Out-Null; "
            + launch
            + "$toast = [Windows.UI.Notifications.ToastNotification]::new($template); "
            "[Windows.UI.Notifications.ToastNotificationManager]::"
            # 差出人として通知に表示される名前（固定値のAPP_IDのみ。検知内容は埋め込まない）
            f"CreateToastNotifier('{APP_ID}').Show($toast)"
        )
        # 管理者権限のPowerShellには、利用者が変えられる環境変数を引き継がない（core/exec_path.child_env）
        env = child_env({"CLAMQTPIE_TOAST_TITLE": title, "CLAMQTPIE_TOAST_BODY": body}, "Windows")
        try:
            subprocess.run(
                [system_tool("powershell", "Windows"), "-NoProfile", "-Command", script],
                check=True,
                capture_output=True,
                timeout=10,
                env=env,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise DesktopNotifyError(detail=str(exc)) from exc

    def _send_linux_notify_send(self, title: str, body: str) -> None:
        # 多くの通知デーモンは本文の一部のマークアップ（<b>・<a href>等）を解釈するため、ファイル名などに
        # 含まれる<・>・&をエスケープして、文字どおりに表示させる
        cmd = [
            system_tool("notify-send", "Linux"),
            html.escape(title, quote=False),
            html.escape(body, quote=False),
            "--urgency=critical",
            # 差出人の名前（固定値）。付けないと通知デーモンに「notify-send」と表示される
            f"--app-name={APP_NAME}",
        ]
        if self._icon_path:
            cmd.append(f"--icon={self._icon_path}")
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=10)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise DesktopNotifyError(detail=str(exc)) from exc

    # -- メール通知（5章） -----------------------------------------------------
    def _send_detection_mail(self, events: list[DetectionEvent]) -> None:
        smtp = self.config.smtp
        if not (smtp.host and smtp.from_address and smtp.to_address):
            logger.warning("SMTP未設定のためメール通知をスキップしました")
            return

        try:
            password = self._config_service.get_smtp_password()
        except KeyringUnavailableError:
            logger.warning("SMTPパスワードを取得できないためメール通知をスキップしました")
            return

        quarantine_status_summary = _quarantine_status_summary(events)

        detections_block = "".join(
            _DETECTION_BLOCK_TEMPLATE.format(
                path=e.path, virus_name=e.virus_name, detected_at=e.detected_at
            )
            for e in events
        )
        body = _MAIL_TEMPLATE.format(
            first_detected_at=events[0].detected_at,
            count=len(events),
            detections_block=detections_block,
            quarantine_status_summary=quarantine_status_summary,
            app_name=APP_NAME,
        )
        subject = f"[{APP_NAME}] ウイルスを{len(events)}件検知しました"

        try:
            self._smtp_send(smtp, password, subject, body)
        except Exception as exc:  # noqa: BLE001
            # 5.3節: リトライは行わずログにERRORとして記録するのみ
            logger.error(
                f"メール通知の送信に失敗しました (件数: {len(events)}, detail: {exc})",
                extra={"error_code": "E7001"},
            )

    def _smtp_send(self, smtp, password: str | None, subject: str, body: str) -> None:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = smtp.from_address
        msg["To"] = smtp.to_address

        # TLSのサーバ証明書を検証する（証明書の正当性とホスト名の一致を確認）。
        # contextを渡さないと、Pythonは証明書を検証しないため、必ず既定のコンテキストを渡す
        context = ssl.create_default_context()
        if smtp.encryption == "ssl":
            server = smtplib.SMTP_SSL(smtp.host, smtp.port, timeout=10, context=context)
        else:
            server = smtplib.SMTP(smtp.host, smtp.port, timeout=10)

        try:
            if smtp.encryption == "starttls":
                server.starttls(context=context)
            if smtp.user and password:
                if smtp.encryption == "none":
                    # 暗号化しない接続では、パスワードが平文で送られるため認証しない
                    # （認証が必要なサーバーでは、ssl か starttls を使う）
                    logger.warning("SMTPの暗号化が「none」のため、認証を行わずに送信します（パスワードを平文で送らないため）")
                else:
                    server.login(smtp.user, password)
            server.sendmail(smtp.from_address, [smtp.to_address], msg.as_string())
        finally:
            server.quit()

    def send_test_mail(self) -> None:
        """設定画面「テスト送信」ボタン用（画面設計書8-2節）"""
        smtp = self.config.smtp
        if not (smtp.host and smtp.from_address and smtp.to_address):
            raise MailSendError(detail="SMTP設定が不足しています")
        try:
            password = self._config_service.get_smtp_password()
        except KeyringUnavailableError as exc:
            raise MailSendError(detail=str(exc)) from exc

        try:
            self._smtp_send(
                smtp,
                password,
                f"[{APP_NAME}] テスト送信",
                f"これは{APP_NAME}からのテスト送信メールです。",
            )
        except Exception as exc:  # noqa: BLE001
            raise MailSendError(detail=str(exc)) from exc
