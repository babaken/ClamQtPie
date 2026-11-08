"""
管理者（admin）の操作の一覧と、確認画面に出す説明（サービス化・Web API設計書 3.2節・10章。段階 12-4c）

トレイGUI は admin の操作を直接 API に送らず、昇格ヘルパー（elevate_main.py）に操作の一覧を渡して実行させる。
- ADMIN_ROUTES: admin のロールが要る API（6章のロール欄。api/routes/write.py と、user では断られるスキャンの依頼）
- 昇格ヘルパーは、この一覧に無い操作を実行しない（一覧の外の API に admin のトークンを送らない）
- describe: 確認画面に出す日本語の説明。昇格ヘルパーが、受け取った操作そのものから作る
  （頼んだ側が書いた説明は使わない。隔離ファイルの名前などは、ヘルパーが admin のトークンで読んだ一覧から引く）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 1 回の昇格でまとめて実行できる操作の数
MAX_OPERATIONS = 200

_ID = r"(\d{1,18})"

#: (メソッド, パスの正規表現)。パスは API_PREFIX（/api/v1）の後ろ
ADMIN_ROUTES: tuple[tuple[str, re.Pattern], ...] = tuple((m, re.compile(f"^{p}$")) for m, p in (
    ("POST", "/clamd/start"),
    ("POST", "/clamd/stop"),
    ("POST", "/diagnostics"),
    ("POST", f"/quarantine/{_ID}/restore"),
    ("DELETE", f"/quarantine/{_ID}"),
    ("POST", "/excludes"),
    ("PATCH", f"/excludes/{_ID}"),
    ("DELETE", f"/excludes/{_ID}"),
    ("PUT", "/realtime"),
    ("POST", "/realtime/targets"),
    ("PATCH", f"/realtime/targets/{_ID}"),
    ("DELETE", f"/realtime/targets/{_ID}"),
    ("POST", "/schedules"),
    ("PUT", f"/schedules/{_ID}"),
    ("PATCH", f"/schedules/{_ID}"),
    ("DELETE", f"/schedules/{_ID}"),
    ("POST", f"/schedules/{_ID}/run"),
    ("PUT", "/config"),
    ("PUT", "/config/smtp-password"),
    ("DELETE", "/config/smtp-password"),
    ("POST", "/notifications/test-mail"),
    ("DELETE", "/logs"),
    # WebUI の ON/OFF・ログインの設定（Bearer の admin だけ。段階 12-5c）
    ("PUT", "/web"),
    ("PUT", "/web/credentials"),
    # 二段階認証の設定・解除（Bearer の admin だけ。段階 12-5d）
    ("POST", "/web/totp"),
    ("DELETE", "/web/totp"),
    ("POST", "/web/unlock"),
    # user のトークンでは断られることのあるもの（フルスキャン・自分のフォルダの外・スケジュールのスキャンの中止）
    ("POST", "/scans"),
    ("POST", "/scans/current/cancel"),
    ("POST", "/quarantine"),
))

#: 一覧に入れるが、それだけでは昇格しないもの（user のトークンで試し、403 のときだけ昇格する）
USER_FIRST: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/scans"), ("POST", "/scans/current/cancel"), ("POST", "/quarantine"),
})


@dataclass
class Operation:
    method: str
    path: str
    body: object = None

    def to_json(self) -> dict:
        return {"method": self.method, "path": self.path, "body": self.body}


def match(method: str, path: str) -> re.Match | None:
    for m, pattern in ADMIN_ROUTES:
        if m == method:
            found = pattern.match(path)
            if found:
                return found
    return None


def is_admin(method: str, path: str) -> bool:
    """昇格して送る操作か（user のトークンで先に試すものは False）"""
    return match(method, path) is not None and (method, path) not in USER_FIRST


def parse_operations(data) -> list[Operation]:
    """昇格ヘルパーが受け取った一覧を確かめる。正しくなければ ValueError"""
    if not isinstance(data, dict) or not isinstance(data.get("operations"), list):
        raise ValueError("operations がありません")
    raw = data["operations"]
    if not 1 <= len(raw) <= MAX_OPERATIONS:
        raise ValueError(f"操作の数が正しくありません（1〜{MAX_OPERATIONS} 件）")
    operations = []
    for item in raw:
        if not isinstance(item, dict) or set(item) - {"method", "path", "body"}:
            raise ValueError("操作の形が正しくありません")
        method, path = item.get("method"), item.get("path")
        if not isinstance(method, str) or not isinstance(path, str) or match(method, path) is None:
            raise ValueError(f"管理者の操作の一覧に無い操作です: {method} {path}"[:300])
        body = item.get("body")
        if body is not None and not isinstance(body, dict):
            raise ValueError("操作の本文が正しくありません")
        operations.append(Operation(method, path, body))
    return operations


# -- 説明 ----------------------------------------------------------------------------------------
@dataclass
class Lookup:
    """説明に使う名前（昇格ヘルパーが admin のトークンで読んだ一覧。読めなければ空）"""

    quarantine: dict[int, dict] = field(default_factory=dict)
    excludes: dict[int, dict] = field(default_factory=dict)
    targets: dict[int, dict] = field(default_factory=dict)
    schedules: dict[int, dict] = field(default_factory=dict)
    config: dict = field(default_factory=dict)


_EXCLUDE_TYPES = {"file": "ファイル", "folder": "フォルダ", "extension": "拡張子", "process": "プロセス"}
_SCAN_TYPES = {"quick": "クイックスキャン", "full": "フルスキャン", "custom": "スキャン"}
_JOB_TYPES = {"scan": "スキャン", "update": "定義の更新"}
_MAX_VALUE = 200


def _short(value) -> str:
    text = str(value)
    return text if len(text) <= _MAX_VALUE else text[:_MAX_VALUE] + "…"


def _onoff(enabled) -> str:
    return "有効" if enabled else "無効"


def _quarantine_name(lookup: Lookup, qid: int) -> str:
    item = lookup.quarantine.get(qid)
    if not item:
        return f"隔離ファイル（ID {qid}）"
    return f"隔離ファイル「{_short(item.get('original_path', ''))}」（{_short(item.get('virus_name', ''))}）"


def _exclude_name(lookup: Lookup, eid: int) -> str:
    item = lookup.excludes.get(eid)
    if not item:
        return f"除外（ID {eid}）"
    return f"除外「{_EXCLUDE_TYPES.get(item.get('type'), item.get('type'))}: {_short(item.get('value', ''))}」"


def _target_name(lookup: Lookup, tid: int) -> str:
    item = lookup.targets.get(tid)
    return f"監視対象「{_short(item.get('path', ''))}」" if item else f"監視対象（ID {tid}）"


def _schedule_name(lookup: Lookup, sid: int) -> str:
    item = lookup.schedules.get(sid)
    return f"スケジュール「{_short(item.get('name', ''))}」" if item else f"スケジュール（ID {sid}）"


def _schedule_text(body: dict) -> str:
    kind = _JOB_TYPES.get(body.get("scan_type"), body.get("scan_type"))
    parts = [kind, f"cron「{_short(body.get('cron', ''))}」"]
    if body.get("target"):
        parts.append(f"対象「{_short(body['target'])}」")
    if body.get("wake"):
        parts.append("スリープ中でも起こして実行する")
    return f"「{_short(body.get('name', ''))}」（{'、'.join(parts)}）"


def _config_changes(old: dict, new: dict, prefix: str = "") -> list[str]:
    changes = []
    for key in sorted(set(old) | set(new)):
        name = f"{prefix}{key}"
        before, after = old.get(key), new.get(key)
        if isinstance(before, dict) and isinstance(after, dict):
            changes += _config_changes(before, after, f"{name}.")
        elif before != after and key not in ("protected", "smtp_password_set"):
            changes.append(f"{name}: {_short(before)} → {_short(after)}")
    return changes


def _minutes_text(minutes: int) -> str:
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    parts = [f"{days} 日" if days else "", f"{hours} 時間" if hours else "", f"{mins} 分" if mins else ""]
    return " ".join(p for p in parts if p) or "0 分"


def _web_timer_text(body: dict) -> str:
    from core.duration_text import local_time_text

    if "off_at" in body and body["off_at"]:
        return f"{local_time_text(str(body['off_at'])) or _short(body['off_at'])} に自動で OFF にします"
    minutes = body.get("off_after_min")
    if isinstance(minutes, int) and not isinstance(minutes, bool):
        return f"{_minutes_text(minutes)}後に自動で OFF にします"
    return "自動では OFF にしません（OFF にするまで使えます）"


def _describe_web(body: dict) -> list[str]:
    timer = "off_at" in body or "off_after_min" in body
    if body.get("enabled") is True:
        if body.get("lan") is True:
            return ["WebUI を ON にします（このPC のブラウザと、LAN からも接続できます。LAN からの接続は試験的）",
                    f"  {_web_timer_text(body)}"]
        return ["WebUI を ON にします（このPC のブラウザから使えるようにします）", f"  {_web_timer_text(body)}"]
    if body.get("enabled") is False:
        return ["WebUI を OFF にします（ログイン中の WebUI はすべてログアウトします）"]
    if body.get("lan") is True:
        return ["WebUI の LAN からの接続を許可します（試験的）"]
    if body.get("lan") is False:
        return ["WebUI の LAN からの接続をやめます（このPC のブラウザだけにします）"]
    if timer:
        return [f"WebUI を OFF にする時刻を変えます: {_web_timer_text(body)}"]
    return ["WebUI の設定を変えます"]


def describe(op: Operation, lookup: Lookup | None = None) -> list[str]:
    """確認画面に出す説明（1 行目が操作、2 行目以降は細目）"""
    lookup = lookup or Lookup()
    found = match(op.method, op.path)
    ident = int(found.group(1)) if found and found.groups() else None
    body = op.body if isinstance(op.body, dict) else {}
    m, p = op.method, op.path
    if (m, p) == ("POST", "/clamd/start"):
        return ["clamd（ClamAV のサービス）を起動します"]
    if (m, p) == ("POST", "/clamd/stop"):
        return ["clamd（ClamAV のサービス）を停止します（停止中はスキャン・リアルタイム監視ができません）"]
    if (m, p) == ("POST", "/diagnostics"):
        return ["ヘルスチェック（診断レポートの作成）を行います"]
    if m == "POST" and p.endswith("/restore") and p.startswith("/quarantine/"):
        target = "上書きして元の場所" if body.get("overwrite") else "元の場所"
        return [f"{_quarantine_name(lookup, ident)}を{target}に復元します"]
    if m == "DELETE" and p.startswith("/quarantine/"):
        return [f"{_quarantine_name(lookup, ident)}を完全に削除します（元に戻せません）"]
    if (m, p) == ("POST", "/quarantine"):
        return [f"ファイル「{_short(body.get('path', ''))}」を隔離します（{_short(body.get('virus_name', ''))}）"]
    if (m, p) == ("POST", "/excludes"):
        kind = _EXCLUDE_TYPES.get(body.get("type"), body.get("type"))
        return [f"除外を追加します: {kind}「{_short(body.get('value', ''))}」"]
    if m == "PATCH" and p.startswith("/excludes/"):
        return [f"{_exclude_name(lookup, ident)}を{_onoff(body.get('enabled'))}にします"]
    if m == "DELETE" and p.startswith("/excludes/"):
        return [f"{_exclude_name(lookup, ident)}を削除します"]
    if (m, p) == ("PUT", "/realtime"):
        return ["リアルタイム監視を開始します" if body.get("enabled") else "リアルタイム監視を停止します"]
    if (m, p) == ("POST", "/realtime/targets"):
        return [f"監視対象を追加します: 「{_short(body.get('path', ''))}」"]
    if m == "PATCH" and p.startswith("/realtime/targets/"):
        return [f"{_target_name(lookup, ident)}を{_onoff(body.get('enabled'))}にします"]
    if m == "DELETE" and p.startswith("/realtime/targets/"):
        return [f"{_target_name(lookup, ident)}を削除します"]
    if (m, p) == ("POST", "/schedules"):
        return [f"スケジュールを追加します: {_schedule_text(body)}"]
    if m == "PUT" and p.startswith("/schedules/"):
        return [f"{_schedule_name(lookup, ident)}を変更します: {_schedule_text(body)}"]
    if m == "PATCH" and p.startswith("/schedules/"):
        return [f"{_schedule_name(lookup, ident)}を{_onoff(body.get('enabled'))}にします"]
    if m == "DELETE" and p.startswith("/schedules/"):
        return [f"{_schedule_name(lookup, ident)}を削除します"]
    if m == "POST" and p.endswith("/run"):
        return [f"{_schedule_name(lookup, ident)}を今すぐ実行します"]
    if (m, p) == ("PUT", "/config"):
        changes = _config_changes(lookup.config, body) if lookup.config else []
        return ["設定を変更します", *(f"  {c}" for c in changes)] if changes else ["設定を保存します"]
    if (m, p) == ("PUT", "/config/smtp-password"):
        return ["SMTP のパスワードを変更します"]
    if (m, p) == ("DELETE", "/config/smtp-password"):
        return ["SMTP のパスワードを削除します"]
    if (m, p) == ("POST", "/notifications/test-mail"):
        return ["テストメールを送ります"]
    if (m, p) == ("DELETE", "/logs"):
        return ["ログを消去します（元に戻せません）"]
    if (m, p) == ("PUT", "/web"):
        return _describe_web(body)
    if (m, p) == ("PUT", "/web/credentials"):
        # パスワードは確認画面に出さない
        return [f"WebUI のログインを設定します（ユーザー名: {_short(body.get('username', ''))}）",
                "  ログイン中の WebUI はすべてログアウトします"]
    if (m, p) == ("POST", "/web/totp"):
        # 秘密鍵・コードは確認画面に出さない
        return ["WebUI の二段階認証を設定します", "  ログイン中の WebUI はすべてログアウトします"]
    if (m, p) == ("DELETE", "/web/totp"):
        return ["WebUI の二段階認証を解除します", "  ログイン中の WebUI はすべてログアウトします"]
    if (m, p) == ("POST", "/web/unlock"):
        return ["WebUI のログインの停止を解きます（確認コードの間違いが続いたため止めていたもの）",
                "  パスワードが漏れているおそれがあるときは、先にパスワードを変えてください"]
    if (m, p) == ("POST", "/scans"):
        kind = _SCAN_TYPES.get(body.get("type"), "スキャン")
        targets = body.get("targets") or []
        lines = [f"{kind}を開始します"]
        lines += [f"  対象: {_short(t)}" for t in targets[:20]]
        if len(targets) > 20:
            lines.append(f"  ほかに {len(targets) - 20} 件")
        return lines
    if (m, p) == ("POST", "/scans/current/cancel"):
        return ["実行中のスキャンを中止します"]
    return [f"{m} {p}"]
