"""
WebUI の Cookie セッションと総当たり対策（サービス化・Web API設計書 3.3節。段階 12-5a）

- セッション ID は 32 バイトの乱数。ログインのたびに新しく作り、要求に付いていた古い Cookie は使わない
- CSRF トークンはセッションに結び付けてサービス側で持つ（Cookie にはしない。別のポートのサーバーから Cookie を
  上書きされても破られないように。12章 29）
- 有効期限は最後の操作から api.session_timeout_min 分
- admin の操作の前の再確認（POST /auth/reauth）に成功すると、そのセッションを 5 分間「確認済み」にする
- 同じ接続元から 5 回続けて失敗したら、その接続元のログインを 5 分間受け付けない（E8106）。全体のロック
  （LAN からのログインだけが対象）は LAN からの接続（段階 12-5e）とあわせて作る
- 状態はメモリだけに持つ（サービスを再起動したら、ログインし直す）
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass

#: admin の操作の前の再確認が有効な時間
REAUTH_SEC = 5 * 60
#: 同じ接続元からの、続けての失敗の上限と、ロックする時間
MAX_FAILURES = 5
LOCK_SEC = 5 * 60
#: 同時に持つセッションの上限（ログインを繰り返してメモリを使わせない。古いものから捨てる）
MAX_SESSIONS = 64


def _key(session_id: str) -> str:
    # 辞書の照合の時間でセッション ID を推測させないよう、ハッシュで引く
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


@dataclass
class WebSession:
    id: str
    csrf_token: str
    client: str
    last_seen: float
    reauth_until: float = 0.0


class WebSessions:
    def __init__(self, timeout_min, clock=time.monotonic) -> None:
        """timeout_min: 有効期限（分）を返す関数（設定を変えたらすぐ効くように、毎回読む）"""
        self._timeout_min = timeout_min
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[str, WebSession] = {}

    def create(self, client: str) -> WebSession:
        now = self._clock()
        session = WebSession(secrets.token_urlsafe(32), secrets.token_urlsafe(32), client, now)
        with self._lock:
            self._drop_expired(now)
            while len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions, key=lambda k: self._sessions[k].last_seen)
                del self._sessions[oldest]
            self._sessions[_key(session.id)] = session
        return session

    def get(self, session_id: str | None) -> WebSession | None:
        """有効なセッション（期限が切れていれば消して None）。見つかれば最後の操作の時刻を更新する"""
        if not session_id:
            return None
        now = self._clock()
        with self._lock:
            session = self._sessions.get(_key(session_id))
            if session is None:
                return None
            if self._expired(session, now):
                del self._sessions[_key(session_id)]
                return None
            session.last_seen = now
            return session

    def destroy(self, session_id: str | None) -> None:
        if session_id:
            with self._lock:
                self._sessions.pop(_key(session_id), None)

    def clear(self) -> int:
        """すべてのセッションを破棄する（パスワードの変更・WebUI の OFF）。破棄した数"""
        with self._lock:
            count = len(self._sessions)
            self._sessions.clear()
        return count

    def mark_reauthenticated(self, session: WebSession) -> None:
        session.reauth_until = self._clock() + REAUTH_SEC

    def is_reauthenticated(self, session: WebSession) -> bool:
        return self._clock() < session.reauth_until

    @property
    def count(self) -> int:
        with self._lock:
            self._drop_expired(self._clock())
            return len(self._sessions)

    def _expired(self, session: WebSession, now: float) -> bool:
        return now - session.last_seen > self._timeout_min() * 60

    def _drop_expired(self, now: float) -> None:
        for key in [k for k, s in self._sessions.items() if self._expired(s, now)]:
            del self._sessions[key]


class LoginGuard:
    """接続元ごとの、続けての失敗の回数とロック"""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, int] = {}
        self._locked_until: dict[str, float] = {}

    def locked_seconds(self, client: str) -> int:
        """ロック中なら残りの秒数（0 ならロックしていない）"""
        with self._lock:
            until = self._locked_until.get(client, 0.0)
            remaining = until - self._clock()
            if remaining <= 0:
                self._locked_until.pop(client, None)
                return 0
            return int(remaining) + 1

    def failure(self, client: str) -> bool:
        """失敗を数える。この失敗でロックしたら True"""
        with self._lock:
            count = self._failures.get(client, 0) + 1
            if count >= MAX_FAILURES:
                self._failures.pop(client, None)
                self._locked_until[client] = self._clock() + LOCK_SEC
                return True
            self._failures[client] = count
            if len(self._failures) > 4096:  # 接続元を変えて数を増やされても、メモリを使い続けない
                self._failures.clear()
            return False

    def success(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)
