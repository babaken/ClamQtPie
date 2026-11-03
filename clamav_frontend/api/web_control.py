"""
WebUI の待ち受けと設定（サービス化・Web API設計書 3.1節・3.3節・6.11節。段階 12-5a）

- WebUI の待ち受けは、内部 API と同じルーターを 2 つ目の uvicorn で動かす（api.app.create_app の listener="web"）。
  api.web_enabled が ON のときだけ待ち受け、OFF にしたら待ち受け自体を止め、セッションをすべて破棄する
- 待ち受けに失敗しても、サービスは止めずに WebUI を OFF にして通知する（9.4節）
- 時間を決めて ON にした場合（api.web_off_at）、時刻になったら OFF にして通知する（サービスを再起動しても引き継ぐ）
- WebUI の ON/OFF・ログインの設定（PUT /web・PUT /web/credentials）は admin の Bearer だけが変えられる
  （api.auth.require_bearer_admin）。処理はメインスレッドで行う（invoker）
- LAN からの接続（lan）は、HTTPS と二段階認証（段階 12-5d・12-5e）ができるまで受け付けない（400）
"""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone

from api.server import LOOPBACK_HOST, ApiServer
from api.web_sessions import LoginGuard, WebSessions
from core.duration_text import check_off_at, local_time_text, off_at_after, parse_off_at
from core.exceptions import ConfigValidationError
from core.logger import CATEGORY_SYSTEM, log_event
from core.web_password import hash_password, password_problem, username_problem

logger = logging.getLogger("clamav_frontend")


# -- 設定の変え方（WebController と、サービスが止まっているときの clamqtpie-ctl web の両方で使う） --------------
def web_changes(api, body: dict) -> dict:
    """PUT /web の本文から、変える api.* の値を作る。受け付けられなければ ConfigValidationError"""
    if body.get("lan"):
        raise ConfigValidationError("LAN からの接続は、HTTPS と二段階認証の設定の後に使えます（未対応）", detail="lan")
    enabled = body.get("enabled")
    enabled = api.web_enabled if enabled is None else enabled
    if enabled and not api.web_password_hash:
        raise ConfigValidationError("先に WebUI のログイン（ユーザー名・パスワード）を設定してください", detail="password")
    changes: dict = {"web_enabled": bool(enabled)}
    if "off_at" in body and "off_after_min" in body:
        raise ConfigValidationError("off_at と off_after_min はどちらか一方だけを指定してください", detail="off_at")
    try:
        if "off_after_min" in body:
            minutes = body["off_after_min"]
            changes["web_off_at"] = "" if minutes is None else off_at_after(minutes)
        elif "off_at" in body:
            changes["web_off_at"] = "" if body["off_at"] is None else check_off_at(body["off_at"])
    except ValueError as exc:
        raise ConfigValidationError(str(exc), detail="off_at") from None
    if not changes["web_enabled"]:
        changes["web_off_at"] = ""  # OFF にしたら期限も消す
    return changes


def credential_changes(username, password) -> dict:
    problem = username_problem(username) or password_problem(password)
    if problem:
        raise ConfigValidationError(problem, detail="credentials")
    return {"web_username": username, "web_password_hash": hash_password(password),
            "web_password_length": len(password)}


def web_state_text(api) -> str:
    when = f"（{local_time_text(api.web_off_at)} に OFF）" if api.web_enabled and api.web_off_at else ""
    return f"{'ON' if api.web_enabled else 'OFF'}{when}"


class WebController:
    def __init__(self, context, tokens, server_factory=ApiServer) -> None:
        self._context = context
        self._tokens = tokens
        self._server_factory = server_factory
        config_service = context.config_service
        self.sessions = WebSessions(lambda: config_service.current.api.session_timeout_min)
        self.guard = LoginGuard()
        self.server = None

    # -- 状態 ------------------------------------------------------------------------------------
    @property
    def _api(self):
        return self._context.config_service.current.api

    @property
    def is_running(self) -> bool:
        return self.server is not None and self.server.is_running

    def status(self) -> dict:
        """GET /web の内容（パスワードのハッシュは返さない）"""
        api = self._api
        port = self.server.port if self.is_running else api.web_port
        return {
            "enabled": bool(api.web_enabled),
            "running": self.is_running,
            "lan": False,
            "urls": [f"http://{LOOPBACK_HOST}:{port}/web/login"],
            "username": api.web_username,
            "password_set": bool(api.web_password_hash),
            "totp_set": False,
            "tls": "off",
            "off_at": api.web_off_at or None,
            "session_timeout_min": api.session_timeout_min,
        }

    # -- 待ち受け ----------------------------------------------------------------------------------
    def apply(self) -> None:
        """設定に合わせて待ち受けを始める・止める（メインスレッドで呼ぶ）"""
        api = self._api
        if not api.web_enabled:
            if self.server is not None:
                self.stop()
            return
        if self.server is not None and self.server.port != api.web_port:
            self.stop()
        if self.server is None:
            self.start()

    def start(self) -> None:
        from api.app import create_app

        port = self._api.web_port
        app = create_app(self._tokens, self._context, listener="web", web=self)
        server = self._server_factory(app, port, name="WebUI", event_tag="web")
        try:
            server.start()
        except (OSError, RuntimeError) as exc:
            server.stop()
            logger.warning(f"WebUI の待ち受けを開始できませんでした（{LOOPBACK_HOST}:{port}）。WebUI を OFF にします "
                           f"(detail: {exc})", extra={"error_code": "E8100"})
            self._save(web_enabled=False, web_off_at="")
            self._notify("WebUI を開始できませんでした",
                         f"ポート {port} で待ち受けられなかったため、WebUI を OFF にしました")
            return
        self.server = server
        log_event(CATEGORY_SYSTEM, f"WebUI の待ち受けを開始しました（{LOOPBACK_HOST}:{server.port}）")

    def stop(self) -> None:
        dropped = self.sessions.clear()
        if self.server is not None:
            self.server.stop()
            self.server = None
            log_event(CATEGORY_SYSTEM, f"WebUI の待ち受けを停止しました（破棄したセッション: {dropped} 件）")

    def check_timer(self, now: datetime | None = None) -> bool:
        """OFF にする時刻を過ぎていれば OFF にする（メインスレッドで、定期的に呼ぶ）。OFF にしたら True"""
        api = self._api
        off_at = parse_off_at(api.web_off_at)
        if not api.web_enabled or off_at is None:
            return False
        if off_at > (now or datetime.now(timezone.utc)):
            return False
        self._save(web_enabled=False, web_off_at="")
        self.apply()
        log_event(CATEGORY_SYSTEM, "決めた時刻になったため、WebUI を OFF にしました")
        self._notify("WebUI を OFF にしました", "決めた時刻になったため、WebUI を OFF にしました")
        return True

    # -- 設定（PUT /web・PUT /web/credentials。メインスレッドで呼ぶ） -------------------------------
    def update(self, body: dict) -> dict:
        api = self._api
        changes = web_changes(api, body)
        before = (api.web_enabled, api.web_off_at)
        self._save(**changes)
        self.apply()
        after = self._api
        if (after.web_enabled, after.web_off_at) != before:
            log_event(CATEGORY_SYSTEM, f"Web API: WebUI を {web_state_text(after)} にしました")
        return self.status()

    def set_credentials(self, username, password) -> dict:
        self._save(**credential_changes(username, password))
        dropped = self.sessions.clear()
        log_event(CATEGORY_SYSTEM, f"Web API: WebUI のログインを設定しました（ユーザー名: {username}、"
                                   f"破棄したセッション: {dropped} 件）")
        return self.status()

    def _save(self, **values) -> None:
        config_service = self._context.config_service
        model = copy.deepcopy(config_service.current)
        for name, value in values.items():
            setattr(model.api, name, value)
        config_service.save(model)

    def _notify(self, title: str, body: str) -> None:
        notifier = getattr(self._context, "notification_service", None)
        if notifier is None:
            return
        try:
            notifier.notify_system(title, body)
        except Exception as exc:  # noqa: BLE001 - 知らせられなくても WebUI の操作は続ける
            logger.warning(f"WebUI のことを知らせられませんでした (detail: {exc})")
