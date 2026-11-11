"""
WebUI の待ち受けと設定（サービス化・Web API設計書 3.1節・3.3節・6.11節。段階 12-5a）

- WebUI の待ち受けは、内部 API と同じルーターを 2 つ目の uvicorn で動かす（api.app.create_app の listener="web"）。
  api.web_enabled が ON のときだけ待ち受け、OFF にしたら待ち受け自体を止め、セッションをすべて破棄する
- 待ち受けに失敗しても、サービスは止めずに WebUI を OFF にして通知する（9.4節）
- 時間を決めて ON にした場合（api.web_off_at）、時刻になったら OFF にして通知する（サービスを再起動しても引き継ぐ）
- WebUI の ON/OFF・ログインの設定（PUT /web・PUT /web/credentials）は admin の Bearer だけが変えられる
  （api.auth.require_bearer_admin）。処理はメインスレッドで行う（invoker）
- LAN からの接続（段階 12-5e2。試験的。core/web_lan.py）: api.web_lan が true で HTTPS で待ち受けるときは、ループバックと
  LAN の各アドレスで待ち受ける（Windows はファイアウォールの規則も作る）。許可の 3 条件（HTTPS・二段階認証・12 文字以上の
  パスワード）を満たさなければ 400。HTTPS の無効化・二段階認証の解除・短いパスワード・CA の期限切れで OFF に戻す。
  LAN のアドレスが変わったら（1 分ごとに確かめる）待ち受けを始め直す
- 二段階認証（段階 12-5d）: 秘密鍵は頼む側（トレイGUI・clamqtpie-ctl）が作って QR コードを見せ、利用者が認証アプリの
  コードを入れた後で、秘密鍵とコードを一緒に POST /web/totp に送る（昇格を 1 回で済ませるため。サービスは仮の秘密鍵を
  持たない）。コードが合えば有効にし、予備のコードを返す（この応答でだけ）。設定・解除したらセッションをすべて破棄する
- HTTPS（段階 12-5e1。api.web_tls == "local_ca"）: ローカル CA（core/local_ca.py）で作ったサーバー証明書で待ち受ける。
  待ち受けを始めるときと 1 日 1 回、名前・アドレスの変化と期限を確かめ、作り直したら待ち受けを始め直す。CA は自動では
  作り直さない（期限・サブネットの変化は status の tls_info.notes で知らせる）。HTTPS の ON/OFF・CA の作り直しは
  PUT /web/tls・POST /web/tls/reset-ca（admin の Bearer だけ）
- 「パスワードは合っていて確認コードだけが違う」ログインを数え（api.web_totp_failures）、totp.STOP_FAILURES 回になったら、
  管理者が解くまで（POST /web/unlock・パスワードの変更）パスワードと確認コードでのログインを止めて通知する。
  時間・ログインの成功・再起動では戻さない（パスワードが漏れた後のコードの総当たりを防ぐ）
"""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from api.server import LOOPBACK_HOST, ApiServer
from api.web_sessions import LoginGuard, WebSessions
from core.duration_text import check_off_at, local_time_text, off_at_after, parse_off_at
from core.exceptions import ApiAuthError, ApiLoginLockedError, ConfigValidationError
from core.logger import CATEGORY_SYSTEM, log_event
from core.text_safety import visible_controls
from core import totp
from core.config_model import WEB_TLS_MODES
from core.local_ca import CA_CERT, SERVER_CERT, SERVER_KEY, LocalCa
from core.web_lan import MIN_PASSWORD_LENGTH, LanScope, lan_problems, session_timeout_min
from core.web_password import hash_password, password_problem, username_problem

logger = logging.getLogger("clamav_frontend")


# -- 設定の変え方（WebController と、サービスが止まっているときの clamqtpie-ctl web の両方で使う） --------------
def web_changes(api, body: dict) -> dict:
    """PUT /web の本文から、変える api.* の値を作る。受け付けられなければ ConfigValidationError"""
    lan = body.get("lan")
    if lan:
        problems = lan_problems(api)
        if problems:
            raise ConfigValidationError(f"LAN からの接続を許可できません: {'、'.join(problems)}", detail="lan")
    enabled = body.get("enabled")
    enabled = api.web_enabled if enabled is None else enabled
    if enabled and not api.web_password_hash:
        raise ConfigValidationError("先に WebUI のログイン（ユーザー名・パスワード）を設定してください"
                                    "（トレイGUI か clamqtpie-ctl web set-login）", detail="password")
    changes: dict = {"web_enabled": bool(enabled)}
    if lan is not None:
        changes["web_lan"] = bool(lan)
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
    # パスワードを変えたら、確認コードの間違いの回数も 0 に戻す（ログインの停止を解く）
    changes = {"web_username": username, "web_password_hash": hash_password(password),
               "web_password_length": len(password), "web_totp_failures": 0}
    if len(password) < MIN_PASSWORD_LENGTH:
        changes["web_lan"] = False  # LAN からの接続の条件（12 文字以上）を満たさなくなる
    return changes


def _save_api(config_service, **values) -> None:
    model = copy.deepcopy(config_service.current)
    for name, value in values.items():
        setattr(model.api, name, value)
    config_service.save(model)


def enable_totp(config_service, secret, code, now: float | None = None) -> list[str]:
    """二段階認証を有効にする。code が secret の今のコードに合わなければ ConfigValidationError。予備のコードを返す
    （WebController と、サービスが止まっているときの clamqtpie-ctl web totp setup の両方で使う）"""
    try:
        secret = totp.normalize_secret(secret)
    except ValueError as exc:
        raise ConfigValidationError(str(exc), detail="secret") from None
    step = totp.match_step(secret, code, -1, now)
    if step is None:
        raise ConfigValidationError("確認コードが違います。認証アプリに表示されている 6 桁のコードを入れてください"
                                    "（コードが合わないときは、この PC とスマートフォンの時刻が合っているかも確かめてください）",
                                    detail="code")
    codes = totp.generate_recovery_codes()
    config_service.set_totp_secret(secret)
    _save_api(config_service, web_totp_enabled=True, web_totp_last_step=step,
              web_recovery_code_hashes=totp.hash_recovery_codes(codes), web_totp_failures=0)
    return codes


def disable_totp(config_service) -> None:
    """二段階認証を解除する（LAN からの接続の条件を満たさなくなるので、許可も OFF にする）"""
    _save_api(config_service, web_totp_enabled=False, web_totp_last_step=0, web_recovery_code_hashes=[],
              web_totp_failures=0, web_lan=False)
    config_service.delete_totp_secret()


def login_stopped(api) -> bool:
    """確認コードの間違いが続いたため、パスワードと確認コードでのログインを止めているか"""
    return bool(api.web_totp_enabled) and api.web_totp_failures >= totp.STOP_FAILURES


def record_code_failure(config_service) -> int:
    """パスワードは合っていて確認コードだけが違ったログインを数える（設定を保存するため、メインスレッドで呼ぶ）。
    数えた後の回数を返す（STOP_FAILURES より大きくはしない）"""
    count = min(config_service.current.api.web_totp_failures + 1, totp.STOP_FAILURES)
    _save_api(config_service, web_totp_failures=count)
    return count


def unlock_login(config_service) -> bool:
    """確認コードの間違いの回数を 0 に戻す（ログインの停止を解く）。止めていたら True"""
    api = config_service.current.api
    was_stopped = login_stopped(api)
    if api.web_totp_failures:
        _save_api(config_service, web_totp_failures=0)
    return was_stopped


def check_second_factor(config_service, code, now: float | None = None):
    """二段階認証のコード（予備のコードも可）を確かめる。設定は変えない（重い照合を画面・メインスレッドの外で行うため。
    受け付けたことの記録は consume_second_factor）。合えば ("totp", ステップ番号) か ("recovery", ハッシュ)、
    合わなければ None"""
    api = config_service.current.api
    secret = config_service.get_totp_secret()
    if not secret:
        logger.error("二段階認証の秘密鍵を読めません。clamqtpie-ctl web totp reset で解除し、設定し直してください",
                     extra={"error_code": "E8100"})
        return None
    if totp.looks_like_code(code):
        step = totp.match_step(secret, code, api.web_totp_last_step, now)
        return ("totp", step) if step is not None else None
    index = totp.match_recovery_code(code, list(api.web_recovery_code_hashes))
    return ("recovery", api.web_recovery_code_hashes[index]) if index is not None else None


def consume_second_factor(config_service, factor) -> bool:
    """check_second_factor の結果を使ったことにする（同じコードを二度と受け付けない）。その間にほかの要求が先に
    使っていたら False"""
    api = config_service.current.api
    kind, value = factor
    if kind == "totp":
        if value <= api.web_totp_last_step:
            return False
        _save_api(config_service, web_totp_last_step=value)
        return True
    if value not in api.web_recovery_code_hashes:
        return False
    left = [h for h in api.web_recovery_code_hashes if h != value]
    _save_api(config_service, web_recovery_code_hashes=left)
    log_event(CATEGORY_SYSTEM, f"WebUI の二段階認証で予備のコードを使いました（残り {len(left)} 個）")
    return True


def tls_changes(mode) -> dict:
    """PUT /web/tls の本文から、変える api.* の値を作る"""
    if mode not in WEB_TLS_MODES:
        raise ConfigValidationError("HTTPS の方式は off か local_ca を指定してください", detail="mode")
    if mode == "off":
        return {"web_tls": mode, "web_lan": False}  # LAN からの接続は HTTPS が前提
    return {"web_tls": mode}


def lan_turned_off_text(before, after) -> str:
    """操作で LAN からの接続の許可が OFF になったら、その案内（ログ・ctl の表示に使う）"""
    return "LAN からの接続の許可も OFF にしました（条件を満たさなくなったため）" if before.web_lan and not after.web_lan else ""


#: サーバー証明書の名前・期限を確かめる間隔
_TLS_CHECK_INTERVAL = timedelta(days=1)
#: LAN のアドレスの変化を確かめる間隔
_LAN_CHECK_INTERVAL = timedelta(minutes=1)


def web_state_text(api) -> str:
    when = f"（{local_time_text(api.web_off_at)} に OFF）" if api.web_enabled and api.web_off_at else ""
    return f"{'ON' if api.web_enabled else 'OFF'}{when}"


class WebController:
    def __init__(self, context, tokens, server_factory=ApiServer, tls_dir: Path | None = None, lan=None) -> None:
        """tls_dir: ローカル CA・サーバー証明書の置き場所（ServicePaths.tls_dir。None なら HTTPS を使えない）。
        lan: LAN の範囲・ファイアウォールの関数（テストで差し替える。既定は core.web_lan）"""
        self._context = context
        self._tokens = tokens
        self._server_factory = server_factory
        self.ca = LocalCa(tls_dir) if tls_dir is not None else None
        #: 待ち受けているのが HTTPS か（Cookie の名前・Secure に使う）と、最後に証明書を確かめた時刻
        self.serving_tls = False
        self._tls_checked_at: datetime | None = None
        if lan is None:
            from core import web_lan as lan
        self._lan = lan
        #: LAN から待ち受けている範囲（ループバックだけなら None。auth の接続元・Host の検査に使う）と、知らせること
        self.scope: LanScope | None = None
        self.lan_notes: list[str] = []
        self._lan_checked_at: datetime | None = None
        #: LAN のアドレスで待ち受けられなかったときの範囲（同じ範囲では 1 分ごとに試し直さない）
        self._lan_failed: tuple | None = None
        config_service = context.config_service
        self.sessions = WebSessions(lambda: session_timeout_min(config_service.current.api))
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
        scheme = "https" if api.web_tls != "off" else "http"
        urls = [f"{scheme}://{LOOPBACK_HOST}:{port}/web/login"]
        if self.scope is not None:
            urls += self.scope.urls(port)
        return {
            "enabled": bool(api.web_enabled),
            "running": self.is_running,
            "lan": bool(api.web_lan),
            "lan_listening": self.scope is not None,
            "lan_notes": list(self.lan_notes),
            "urls": urls,
            "username": api.web_username,
            "password_set": bool(api.web_password_hash),
            "totp_set": bool(api.web_totp_enabled),
            "recovery_codes_left": len(api.web_recovery_code_hashes) if api.web_totp_enabled else 0,
            "login_stopped": login_stopped(api),
            "tls": api.web_tls,
            "tls_info": self.tls_info(),
            "off_at": api.web_off_at or None,
            "session_timeout_min": session_timeout_min(api),
        }

    # -- 待ち受け ----------------------------------------------------------------------------------
    def apply(self) -> None:
        """設定に合わせて待ち受けを始める・止める（メインスレッドで呼ぶ）"""
        api = self._api
        if not api.web_enabled:
            if self.server is not None:
                self.stop()
            return
        want_tls = api.web_tls != "off"
        want_lan = bool(api.web_lan) and want_tls
        if self.server is not None and (self.server.port != api.web_port or self.serving_tls != want_tls
                                        or (self.scope is not None) != want_lan):
            self.stop()
        if self.server is None:
            self.start()

    def start(self) -> None:
        from api.app import create_app

        port = self._api.web_port
        tls = self._prepare_tls() if self._api.web_tls != "off" else None
        if tls is False:
            return
        # LAN からの接続は HTTPS のときだけ（_prepare_tls が CA の期限切れで許可を OFF にすることもある）
        scope = self._lan.LanScope.current() if tls and self._api.web_lan else None
        app = create_app(self._tokens, self._context, listener="web", web=self)
        kwargs = {"tls": tls} if tls else {}
        if scope is not None:
            kwargs["hosts"] = scope.listen_hosts
        server = self._server_factory(app, port, name="WebUI", event_tag="web", **kwargs)
        try:
            server.start()
        except (OSError, RuntimeError, ValueError) as exc:
            server.stop()
            if scope is not None:
                # LAN のアドレス（変わった直後など）で待ち受けられなければ、ループバックだけで続ける
                server = self._start_loopback_only(app, port, tls, scope, exc)
                if server is not None:
                    self.server = server
                    self.serving_tls = True
                    return
            where = ", ".join(kwargs.get("hosts", [LOOPBACK_HOST]))
            logger.warning(f"WebUI の待ち受けを開始できませんでした（{where} のポート {port}）。WebUI を OFF にします "
                           f"(detail: {exc})", extra={"error_code": "E8100"})
            self._save(web_enabled=False, web_off_at="")
            self._notify("WebUI を開始できませんでした",
                         f"ポート {port} で待ち受けられなかったため、WebUI を OFF にしました")
            return
        self.server = server
        self.serving_tls = bool(tls)
        self.scope = scope
        self._lan_failed = None
        self._lan_checked_at = datetime.now(timezone.utc)
        scheme = "https" if tls else "http"
        if scope is None:
            self.lan_notes = []
            log_event(CATEGORY_SYSTEM, f"WebUI の待ち受けを開始しました（{scheme}://{LOOPBACK_HOST}:{server.port}）")
            return
        self.lan_notes = self._open_lan(server.port, scope)
        log_event(CATEGORY_SYSTEM, f"WebUI の待ち受けを開始しました（{scheme}://{LOOPBACK_HOST}:{server.port}。LAN からの"
                                   f"接続を許可: {', '.join(scope.listen_hosts[1:]) or 'LAN のアドレスなし'}、サブネット "
                                   f"{', '.join(str(n) for n in scope.networks) or 'なし'}）")
        for note in self.lan_notes:
            logger.warning(f"WebUI の LAN からの接続: {note}", extra={"error_code": "E8100"})

    def _start_loopback_only(self, app, port, tls, scope, error):
        """LAN のアドレスで待ち受けられなかったとき、ループバックだけで待ち受ける。始められなければ None"""
        logger.warning(f"WebUI を LAN のアドレス（{', '.join(scope.listen_hosts[1:])}）で待ち受けられませんでした。"
                       f"このPC のブラウザからだけ使えるようにします (detail: {error})", extra={"error_code": "E8100"})
        server = self._server_factory(app, port, name="WebUI", event_tag="web", tls=tls)
        try:
            server.start()
        except (OSError, RuntimeError, ValueError):
            server.stop()
            return None
        self.scope = None
        self._lan_failed = (scope.addresses, scope.networks)
        self._lan_checked_at = datetime.now(timezone.utc)
        self.lan_notes = ["LAN のアドレスで待ち受けられませんでした（このPC のブラウザからだけ使えます。LAN のアドレスが"
                          "変わったら試し直します）"]
        log_event(CATEGORY_SYSTEM, f"WebUI の待ち受けを開始しました（https://{LOOPBACK_HOST}:{server.port}。LAN からは"
                                   "待ち受けられませんでした）")
        return server

    def _open_lan(self, port: int, scope) -> list[str]:
        """LAN から待ち受けるときのファイアウォールの規則（Windows）と、知らせること"""
        import sys

        notes = []
        if scope.addresses:
            problem = self._lan.open_firewall(port, sys.executable)
            if problem:
                notes.append(f"ファイアウォールの規則（{self._lan.FIREWALL_RULE}）を作れませんでした（{problem}）")
        try:
            notes += self._lan.network_notes(port, scope)
        except Exception as exc:  # noqa: BLE001 - 知らせることを調べられなくても待ち受けは続ける
            logger.warning(f"ネットワークの状態を調べられませんでした (detail: {exc})", extra={"error_code": "E8100"})
        return notes

    def check_lan(self, now: datetime | None = None) -> bool:
        """1 分ごとに LAN のアドレス・サブネットの変化を確かめ、変わったら待ち受けを始め直す（メインスレッドで、
        定期的に呼ぶ）。始め直したら True"""
        now = now or datetime.now(timezone.utc)
        if not (self.is_running and (self.scope is not None or self._lan_failed is not None)):
            return False
        if self._lan_checked_at is not None and now - self._lan_checked_at < _LAN_CHECK_INTERVAL:
            return False
        self._lan_checked_at = now
        try:
            current = self._lan.LanScope.current()
        except Exception as exc:  # noqa: BLE001 - 調べられなければ今の待ち受けを続ける
            logger.warning(f"LAN のアドレスを調べられませんでした (detail: {exc})", extra={"error_code": "E8100"})
            return False
        last = (self.scope.addresses, self.scope.networks) if self.scope is not None else self._lan_failed
        if (current.addresses, current.networks) == last:
            return False
        log_event(CATEGORY_SYSTEM, "LAN のアドレスが変わったため、WebUI の待ち受けを始め直します")
        self._restart_listener()
        return True

    def _prepare_tls(self):
        """HTTPS の証明書を用意する。(サーバー証明書, 鍵) のファイル。用意できなければ WebUI を OFF にして False"""
        problem = None
        if self.ca is None:
            problem = "HTTPS の証明書の置き場所がありません"
        else:
            try:
                if self.ca.ensure():
                    log_event(CATEGORY_SYSTEM, "WebUI の HTTPS のサーバー証明書を作りました")
            except RuntimeError as exc:
                # CA の期限切れ: 前の証明書があれば HTTPS のまま待ち受ける（ブラウザに警告が出る。12-5e の dig Q13）。
                # LAN からの接続の許可は OFF にする
                logger.warning(str(exc), extra={"error_code": "E8100"})
                self._lan_off_for_expired_ca()
                if not self.ca.path(SERVER_CERT).is_file():
                    problem = str(exc)
            except Exception as exc:  # noqa: BLE001 - 証明書を作れなくても、サービスは止めずに WebUI を OFF にする
                problem = f"HTTPS の証明書を作れませんでした (detail: {exc})"
        self._tls_checked_at = datetime.now(timezone.utc)
        if problem:
            logger.warning(f"{problem}。WebUI を OFF にします", extra={"error_code": "E8100"})
            self._save(web_enabled=False, web_off_at="")
            self._notify("WebUI を開始できませんでした", f"{problem}。WebUI を OFF にしました")
            return False
        return str(self.ca.path(SERVER_CERT)), str(self.ca.path(SERVER_KEY))

    def check_tls(self, now: datetime | None = None) -> bool:
        """1 日 1 回、サーバー証明書の名前・アドレス・期限を確かめ、作り直したら待ち受けを始め直す（メインスレッドで、
        定期的に呼ぶ）。始め直したら True"""
        now = now or datetime.now(timezone.utc)
        if not (self.is_running and self.serving_tls and self.ca is not None):
            return False
        if self._tls_checked_at is not None and now - self._tls_checked_at < _TLS_CHECK_INTERVAL:
            return False
        self._tls_checked_at = now
        try:
            renewed = self.ca.ensure(now=now)
        except RuntimeError as exc:
            logger.warning(str(exc), extra={"error_code": "E8100"})
            if self._lan_off_for_expired_ca():
                self._restart_listener()  # ループバックだけで待ち受け直す
                return True
            return False
        except Exception as exc:  # noqa: BLE001 - 作り直せなくても、今の証明書で待ち受けを続ける
            logger.warning(f"WebUI の HTTPS のサーバー証明書を作り直せませんでした (detail: {exc})",
                           extra={"error_code": "E8100"})
            return False
        if not renewed:
            return False
        log_event(CATEGORY_SYSTEM, "名前・アドレスの変化か期限のため、WebUI の HTTPS のサーバー証明書を作り直しました")
        self._restart_listener()
        return True

    def _lan_off_for_expired_ca(self) -> bool:
        """CA の期限が切れたら、LAN からの接続の許可を OFF にして知らせる（12-5e の dig Q13）。OFF にしたら True"""
        if not self._api.web_lan:
            return False
        self._save(web_lan=False)
        logger.warning("WebUI のローカル CA の期限が切れたため、LAN からの接続の許可を OFF にしました",
                       extra={"error_code": "E8100"})
        self._notify("LAN からの接続を OFF にしました",
                     "WebUI のローカル CA の期限が切れたため、LAN からの接続の許可を OFF にしました。CA を作り直して"
                     "（clamqtpie-ctl web tls reset-ca）、各端末に登録し直してから、許可し直してください")
        return True

    def _restart_listener(self) -> None:
        """待ち受けだけを始め直す（セッションは残す）"""
        if self.server is not None:
            self.server.stop()
            self.server = None
        self._close_lan()
        self.start()

    def tls_info(self) -> dict | None:
        """HTTPS の状態（CA のフィンガープリント・期限、サーバー証明書の名前、知らせること）。CA が無ければ None"""
        if self.ca is None or self._api.web_tls == "off" or not self.ca.path(CA_CERT).is_file():
            return None
        status = self.ca.status()
        return {
            "ca_fingerprint": status.ca_fingerprint,
            "ca_not_after": status.ca_not_after.isoformat() if status.ca_not_after else None,
            "ca_expired": status.ca_expired,
            "server_not_after": status.server_not_after.isoformat() if status.server_not_after else None,
            "server_names": status.server_names,
            "notes": status.notes,
        }

    def stop(self) -> None:
        dropped = self.sessions.clear()
        self.serving_tls = False
        if self.server is not None:
            self.server.stop()
            self.server = None
            log_event(CATEGORY_SYSTEM, f"WebUI の待ち受けを停止しました（破棄したセッション: {dropped} 件）")
        self._close_lan()

    def _close_lan(self) -> None:
        if self.scope is not None:
            self._lan.close_firewall()
        self.scope = None
        self._lan_failed = None
        self.lan_notes = []

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
        lan_before = bool(api.web_lan)
        self._save(**changes)
        after = self._api
        if bool(after.web_lan) != lan_before:
            # 許可を変えたら、セッションをすべて破棄する（LAN の端末のセッション・有効期限が変わるため）
            dropped = self.sessions.clear()
            log_event(CATEGORY_SYSTEM, f"Web API: WebUI の LAN からの接続を{'許可しました（試験的）' if after.web_lan else 'やめました'}"
                                       f"（破棄したセッション: {dropped} 件）")
        self.apply()
        if (after.web_enabled, after.web_off_at) != before:
            log_event(CATEGORY_SYSTEM, f"Web API: WebUI を {web_state_text(after)} にしました")
        return self.status()

    def set_credentials(self, username, password) -> dict:
        before = copy.copy(self._api)
        was_stopped = login_stopped(before)
        self._save(**credential_changes(username, password))
        dropped = self.sessions.clear()
        lan_off = lan_turned_off_text(before, self._api)
        log_event(CATEGORY_SYSTEM, f"Web API: WebUI のログインを設定しました（ユーザー名: {username}、"
                                   f"破棄したセッション: {dropped} 件{'、ログインの停止も解きました' if was_stopped else ''}"
                                   f"{'。' + lan_off if lan_off else ''}）")
        self.apply()
        return self.status()

    def set_tls(self, mode) -> dict:
        """PUT /web/tls（メインスレッドで呼ぶ）。方式を変えたら待ち受けを始め直し、セッションをすべて破棄する
        （Cookie の名前・Secure が変わるため）"""
        changes = tls_changes(mode)
        if changes["web_tls"] == self._api.web_tls:
            return self.status()
        before = copy.copy(self._api)
        self._save(**changes)
        dropped = self.sessions.clear()
        self.apply()
        state = "有効" if mode != "off" else "無効"
        lan_off = lan_turned_off_text(before, self._api)
        log_event(CATEGORY_SYSTEM, f"Web API: WebUI の HTTPS を{state}にしました（破棄したセッション: {dropped} 件）"
                                   f"{'。' + lan_off if lan_off else ''}")
        return self.status()

    def renew_server(self) -> dict:
        """POST /web/tls/renew（メインスレッドで呼ぶ）。CA はそのままで、サーバー証明書だけを作り直す。端末で警告を
        越えて作った証明書の例外は、そのときの証明書に結び付くため、作り直すと使われなくなる（CA の登録はやり直さなくてよい）"""
        if self.ca is None:
            raise ConfigValidationError("HTTPS の証明書の置き場所がありません", detail="tls")
        self.ca.remove_server()
        if self.server is not None and self.serving_tls:
            self._restart_listener()
        log_event(CATEGORY_SYSTEM, "Web API: WebUI の HTTPS のサーバー証明書を作り直しました（CA はそのまま）")
        return self.status()

    def reset_ca(self) -> dict:
        """POST /web/tls/reset-ca（メインスレッドで呼ぶ）。ローカル CA とサーバー証明書を消し、HTTPS で待ち受けて
        いれば作り直して待ち受けを始め直す。前の CA のフィンガープリントを返す（各端末から消してもらうため）"""
        if self.ca is None:
            raise ConfigValidationError("HTTPS の証明書の置き場所がありません", detail="tls")
        old = self.ca.status().ca_fingerprint
        self.ca.remove()
        dropped = self.sessions.clear()
        if self.server is not None:
            self._restart_listener()
        log_event(CATEGORY_SYSTEM, f"Web API: WebUI のローカル CA を作り直しました（前の CA: {old or 'なし'}、"
                                   f"破棄したセッション: {dropped} 件）")
        result = self.status()
        result["previous_ca_fingerprint"] = old
        return result

    def unlock(self) -> dict:
        """POST /web/unlock（メインスレッドで呼ぶ）"""
        if unlock_login(self._context.config_service):
            log_event(CATEGORY_SYSTEM, "Web API: WebUI のログインの停止を解きました")
        else:
            log_event(CATEGORY_SYSTEM, "Web API: WebUI のログインの停止を解く操作をしました（止めていませんでした）")
        return self.status()

    def record_code_failure(self, client: str) -> int:
        """パスワードは合っていて確認コードだけが違ったログイン（メインスレッドで呼ぶ）"""
        before = self._api.web_totp_failures
        count = record_code_failure(self._context.config_service)
        logger.warning(f"WebUI のログインで、パスワードは合っていましたが確認コードが違いました。パスワードが漏れている"
                       f"おそれがあります（接続元: {visible_controls(client)}、{count} 回目）",
                       extra={"error_code": ApiAuthError.default_code})
        if count >= totp.STOP_FAILURES > before:
            logger.error(f"確認コードの間違いが {totp.STOP_FAILURES} 回になったため、WebUI のパスワードと確認コードでの"
                         f"ログインを止めました（管理者が解くまで）", extra={"error_code": ApiLoginLockedError.default_code})
            self._notify("WebUI のログインを止めました",
                         f"パスワードは合っていて確認コードだけが違うログインが {totp.STOP_FAILURES} 回になったため、"
                         "WebUI のログインを止めました。パスワードが漏れているおそれがあります。パスワードを変えてください"
                         "（トレイの「WebUI」→「ログイン設定...」か clamqtpie-ctl web set-login。変えると停止も解けます）。"
                         "パスワードを変えずに解くときは、トレイの「WebUI」→「ログインの停止を解く」か clamqtpie-ctl web "
                         "unlock。止めている間も、予備のコードならログインできます。")
        return count

    def notify_global_lock(self) -> None:
        """LAN からのログインを全体でロックしたことを知らせる（メインスレッドで呼ぶ）"""
        self._notify("LAN からの WebUI のログインを止めました",
                     "LAN からの WebUI のログインの失敗が続いたため、15 分間 LAN からのログインを止めました。"
                     "パスワードを狙われているおそれがあります。心当たりがなければ、LAN からの接続をやめてください"
                     "（clamqtpie-ctl web lan off）。このPC のブラウザからは使えます。")

    def enable_totp(self, secret, code) -> dict:
        codes = enable_totp(self._context.config_service, secret, code)
        dropped = self.sessions.clear()
        log_event(CATEGORY_SYSTEM, f"Web API: WebUI の二段階認証を設定しました（破棄したセッション: {dropped} 件）")
        return {"recovery_codes": codes}

    def disable_totp(self) -> dict:
        before = copy.copy(self._api)
        disable_totp(self._context.config_service)
        dropped = self.sessions.clear()
        lan_off = lan_turned_off_text(before, self._api)
        log_event(CATEGORY_SYSTEM, f"Web API: WebUI の二段階認証を解除しました（破棄したセッション: {dropped} 件）"
                                   f"{'。' + lan_off if lan_off else ''}")
        self.apply()
        return self.status()

    def check_second_factor(self, code):
        """重い照合（予備のコードの scrypt）を含むため、スレッドプールで呼ぶ"""
        return check_second_factor(self._context.config_service, code)

    def consume_second_factor(self, factor) -> bool:
        """設定を保存するため、メインスレッドで呼ぶ（invoker）"""
        return consume_second_factor(self._context.config_service, factor)

    def _save(self, **values) -> None:
        _save_api(self._context.config_service, **values)

    def _notify(self, title: str, body: str) -> None:
        notifier = getattr(self._context, "notification_service", None)
        if notifier is None:
            return
        try:
            notifier.notify_system(title, body)
        except Exception as exc:  # noqa: BLE001 - 知らせられなくても WebUI の操作は続ける
            logger.warning(f"WebUI のことを知らせられませんでした (detail: {exc})")
