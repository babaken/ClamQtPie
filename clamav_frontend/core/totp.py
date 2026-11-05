"""
WebUI の二段階認証（TOTP。サービス化・Web API設計書 3.3節。段階 12-5d）

- RFC 6238（HMAC-SHA1、30 秒、6 桁）を標準ライブラリだけで計算する。前後 1 ステップ（±30 秒）のずれまで受け付ける
- 一度受け付けたステップ番号より前・同じものは二度と受け付けない（盗み見たコードの使い回しを防ぐ。
  受け付けたステップ番号は api.web_totp_last_step に残し、サービスを再起動しても引き継ぐ）
- 秘密鍵は 20 バイトの乱数を Base32（32 文字、= なし）にしたもの。設定ファイルには入れず、SMTP パスワードと
  同じ秘密の保存先に置く（core/config_service.py）
- 予備のコードは使い捨てで 10 個（英数字 10 文字。紛らわしい文字を除く）。scrypt のハッシュだけを保存する
- QR コードは segno（pure Python）で作る（トレイGUI は PNG、clamqtpie-ctl は端末の文字）
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

from core.version import APP_NAME

STEP_SEC = 30
DIGITS = 6
#: 前後に受け付けるステップの数
WINDOW = 1
SECRET_BYTES = 20
RECOVERY_COUNT = 10
#: 「パスワードは合っていて確認コードだけが違う」ログインがこの回数になったら、管理者が解くまで、パスワードと確認コード
#: でのログインを止める（予備のコードでのログインは受け付ける）
STOP_FAILURES = 10
RECOVERY_LENGTH = 10
#: 予備のコードの文字（紛らわしい 0 O 1 I L を除く大文字と数字。入力は大文字・小文字を区別しない）
_RECOVERY_ALPHABET = "".join(c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" if c not in "0O1IL")


# -- 秘密鍵 ------------------------------------------------------------------------------------------
def generate_secret() -> str:
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def normalize_secret(secret) -> str:
    """空白・小文字を許して読む（手入力・コピーのため）。正しくなければ ValueError"""
    if not isinstance(secret, str):
        raise ValueError("二段階認証の秘密鍵が正しくありません")
    text = "".join(secret.split()).upper().rstrip("=")
    try:
        key = _decode(text)
    except (binascii.Error, ValueError):
        raise ValueError("二段階認証の秘密鍵が正しくありません") from None
    if len(key) != SECRET_BYTES:
        raise ValueError("二段階認証の秘密鍵が正しくありません")
    return text


def _decode(secret: str) -> bytes:
    return base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)


def machine_name() -> str:
    """認証アプリに登録する名前に使う、この PC の名前（Windows はコンピューター名、Linux はホスト名）。
    複数の PC の WebUI を認証アプリで見分けられるよう、ユーザー名ではなく PC の名前を使う"""
    import os
    import socket
    import sys

    name = os.environ.get("COMPUTERNAME", "") if sys.platform == "win32" else ""
    if not name:
        try:
            name = socket.gethostname()
        except OSError:
            name = ""
    name = "".join(c for c in name.strip() if c.isprintable())[:64]
    return name or "PC"


def otpauth_uri(secret: str, account: str, issuer: str = APP_NAME) -> str:
    """認証アプリに読ませる URI（Key URI Format）。account は認証アプリに出る名前（machine_name()）"""
    label = quote(f"{issuer}:{account}", safe="")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer, safe='')}&algorithm=SHA1&digits={DIGITS}" \
           f"&period={STEP_SEC}"


# -- コード -------------------------------------------------------------------------------------------
def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP_SEC)


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_decode(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** DIGITS)).zfill(DIGITS)


def looks_like_code(code) -> bool:
    return isinstance(code, str) and len(code.strip()) == DIGITS and code.strip().isdigit()


def match_step(secret: str, code, last_step: int, now: float | None = None) -> int | None:
    """code が今の前後 WINDOW ステップのどれかに合い、そのステップが last_step より後なら、そのステップ番号。
    合わなければ None（使ったステップより前・同じものは受け付けない）"""
    if not looks_like_code(code):
        return None
    code = code.strip()
    step = current_step(now)
    found = None
    for candidate in range(step - WINDOW, step + WINDOW + 1):
        # 早く抜けず、すべての候補を比べる（どこで合ったかを時間で見分けさせない）
        if hmac.compare_digest(code_at(secret, candidate), code) and candidate > last_step and found is None:
            found = candidate
    return found


# -- 予備のコード -------------------------------------------------------------------------------------
def generate_recovery_codes(count: int = RECOVERY_COUNT) -> list[str]:
    """表示用は 5 文字ずつハイフンで区切る（入力ではハイフン・空白を無視する）"""
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(RECOVERY_LENGTH))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def normalize_recovery_code(code) -> str | None:
    """入力を照合用の形（大文字・区切りなし）にする。予備のコードの形でなければ None"""
    if not isinstance(code, str):
        return None
    text = "".join(code.split()).replace("-", "").upper()
    if len(text) != RECOVERY_LENGTH or any(c not in _RECOVERY_ALPHABET for c in text):
        return None
    return text


def hash_recovery_codes(codes: list[str]) -> list[str]:
    from core.web_password import hash_password

    return [hash_password(normalize_recovery_code(code)) for code in codes]


def match_recovery_code(code, hashes: list[str]) -> int | None:
    """code が予備のコードのどれかに合えば、その位置。合わなければ None"""
    from core.web_password import verify_password

    text = normalize_recovery_code(code)
    if text is None:
        return None
    found = None
    for index, stored in enumerate(hashes):
        if verify_password(text, stored) and found is None:
            found = index
    return found


# -- QR コード ----------------------------------------------------------------------------------------
def qr_png(uri: str, scale: int = 6) -> bytes:
    import io

    import segno

    buffer = io.BytesIO()
    segno.make(uri, error="m").save(buffer, kind="png", scale=scale, border=4, dark="black", light="white")
    return buffer.getvalue()


def qr_text(uri: str) -> str:
    """端末に表示する QR コード（上下 2 画素を 1 文字にまとめる。暗い背景の端末でも読めるよう、白地に黒で描く）"""
    import segno

    matrix = [list(row) for row in segno.make(uri, error="m").matrix]
    border = 4
    width = len(matrix[0]) + border * 2
    rows = [[0] * width for _ in range(border)]
    rows += [[0] * border + [1 if cell else 0 for cell in row] + [0] * border for row in matrix]
    rows += [[0] * width for _ in range(border)]
    if len(rows) % 2:
        rows.append([0] * width)
    lines = []
    for top, bottom in zip(rows[0::2], rows[1::2]):
        # 1 が黒。白地に黒で描くため、白い部分を文字で塗る（█ 上下とも白、▀ 上だけ白、▄ 下だけ白、空白は両方黒）
        lines.append("".join({(0, 0): "█", (0, 1): "▀", (1, 0): "▄", (1, 1): " "}[(t, b)]
                             for t, b in zip(top, bottom)))
    return "\n".join(lines)
