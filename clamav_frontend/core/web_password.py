"""
WebUI のログインのパスワード（サービス化・Web API設計書 3.3節。段階 12-5a）

- パスワードは hashlib.scrypt（ソルト 16 バイト、n=2^14, r=8, p=1）のハッシュだけを保存する。平文はどこにも残さない
- 照合は hmac.compare_digest。ハッシュが無い・壊れている・ユーザー名が違う場合も、同じだけ計算してから
  失敗にする（どちらが違ったかを時間で見分けられないようにする）
- 長さは 8 文字以上（LAN 許可中は 12 文字以上。3.1節）、1024 文字まで。ユーザー名は 1〜32 文字の英数字と _.-
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import threading

_SCHEME = "scrypt"
_N, _R, _P = 2 ** 14, 8, 1
_SALT_BYTES = 16
_KEY_BYTES = 32
#: 保存されたハッシュの作り方として受け付ける範囲（設定ファイルを書き換えて、照合に時間・メモリを使わせない）
_MAX_N = 2 ** 16
_MAXMEM = 128 * 1024 * 1024

MIN_LENGTH = 8
MIN_LENGTH_LAN = 12
MAX_LENGTH = 1024
_USERNAME = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")
#: 自動生成のパスワードの文字（紛らわしい 0 O o 1 l I を除く。3.4節）
_GENERATE_ALPHABET = "".join(c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
                             if c not in "0Oo1lI")
GENERATED_LENGTH = 20

#: ハッシュが無いときに照合の時間をそろえるための、使い捨てのハッシュ
_DUMMY: str | None = None
#: 同時に行う照合の数の上限（ログインの要求を一度にたくさん送られても、失敗の回数でロックする前に scrypt の
#: 計算・メモリ（1 回 16 MB）を使い切らせない。超えた分は順番を待つ）
_VERIFY_SLOTS = threading.BoundedSemaphore(2)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _derive(password: str, salt: bytes, n: int, r: int, p: int, length: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=length, maxmem=_MAXMEM)


def hash_password(password: str) -> str:
    salt = os.urandom(_SALT_BYTES)
    key = _derive(password, salt, _N, _R, _P, _KEY_BYTES)
    return f"{_SCHEME}${_N}${_R}${_P}${_b64(salt)}${_b64(key)}"


def _parse(stored: str):
    try:
        scheme, n, r, p, salt, key = stored.split("$")
        n, r, p = int(n), int(r), int(p)
        salt, key = base64.b64decode(salt, validate=True), base64.b64decode(key, validate=True)
    except (ValueError, AttributeError):
        return None
    if scheme != _SCHEME or not (2 <= n <= _MAX_N) or n & (n - 1) or not (1 <= r <= 16) or not (1 <= p <= 4):
        return None
    if not (8 <= len(salt) <= 64) or not (16 <= len(key) <= 64):
        return None
    return n, r, p, salt, key


def is_valid_hash(stored: str) -> bool:
    return bool(stored) and _parse(stored) is not None


def verify_password(password: str, stored: str) -> bool:
    """password が stored（hash_password の結果）と一致するか。stored が空・壊れていても同じだけ計算して False"""
    with _VERIFY_SLOTS:
        return _verify(password, stored)


def _verify(password: str, stored: str) -> bool:
    global _DUMMY
    parsed = _parse(stored) if stored else None
    if parsed is None:
        if _DUMMY is None:
            _DUMMY = hash_password(secrets.token_urlsafe(16))
        parsed = _parse(_DUMMY)
        n, r, p, salt, key = parsed
        hmac.compare_digest(_derive(password, salt, n, r, p, len(key)), key)
        return False
    n, r, p, salt, key = parsed
    try:
        derived = _derive(password, salt, n, r, p, len(key))
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(derived, key)


def username_problem(name) -> str | None:
    if not isinstance(name, str) or not _USERNAME.match(name):
        return "ユーザー名は 1〜32 文字の英数字と _ . - で指定してください"
    return None


def password_problem(password, lan: bool = False) -> str | None:
    minimum = MIN_LENGTH_LAN if lan else MIN_LENGTH
    if not isinstance(password, str) or len(password) < minimum:
        where = "（LAN からの接続を許可するときは 12 文字以上）" if not lan else ""
        return f"パスワードは {minimum} 文字以上にしてください{where}"
    if len(password) > MAX_LENGTH:
        return f"パスワードは {MAX_LENGTH} 文字以内にしてください"
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in password):
        return "パスワードに制御文字は使えません"
    return None


def generate_password(length: int = GENERATED_LENGTH) -> str:
    return "".join(secrets.choice(_GENERATE_ALPHABET) for _ in range(length))


def usernames_match(entered: str, stored: str) -> bool:
    return hmac.compare_digest(str(entered).encode("utf-8"), str(stored).encode("utf-8"))
