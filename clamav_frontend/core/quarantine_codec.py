"""
隔離ファイルの変換（サービス化・Web API設計書 6.3節、12章 45。段階 12-2d）

隔離したファイルを元の内容のまま置いておくと、他のウイルス対策ソフトが隔離フォルダのファイルを検知して
削除・警告したり、バックアップや同期で外へ持ち出されたり、誤って実行されたりする。そのため、
先頭に識別用のヘッダを付け、中身を固定の鍵で XOR して保存する。
目的は他のソフトに誤って実行・検知されないことで、暗号としての強さではない（鍵は秘密にしない）。

形式（xor-v1）: b"CQPQ" + 形式の番号（1 バイト）+ 元の大きさ（8 バイト、ビッグエンディアン）+ 元の SHA-256（32 バイト）
             + 中身（ファイルの先頭からの位置で鍵を繰り返して XOR したもの）
"""
from __future__ import annotations

import hashlib
import os

ENCODING_NONE = "none"
ENCODING_XOR_V1 = "xor-v1"

MAGIC = b"CQPQ"
VERSION = 1
HEADER_SIZE = len(MAGIC) + 1 + 8 + 32
#: 固定の鍵（秘密にしない。変えると既存の隔離ファイルを戻せなくなるため変えない）
KEY = bytes.fromhex("5a3c96e1d27f0b48a5c3e91f6d2b7084c1e85f3a9d06b27e4fa1c8350e9d6b2f")
#: 一度に変換する大きさ（鍵の長さの倍数）
_CHUNK = len(KEY) * 32768


class QuarantineFormatError(ValueError):
    """隔離ファイルの形式が正しくない・中身が記録と一致しない"""


def _xor(chunk: bytes) -> bytes:
    # 1 バイトずつではなく、大きな整数としてまとめて XOR する（大きなファイルでも遅くならないように）
    key = (KEY * (len(chunk) // len(KEY) + 1))[: len(chunk)]
    return (int.from_bytes(chunk, "big") ^ int.from_bytes(key, "big")).to_bytes(len(chunk), "big")


def encode_file(src: str | os.PathLike, dest: str | os.PathLike, sha256_hex: str, size: int) -> None:
    """src（隔離フォルダに移した元のファイル）を変換して dest に書く。dest は新しく作る（既にあれば失敗）"""
    header = MAGIC + bytes([VERSION]) + size.to_bytes(8, "big") + bytes.fromhex(sha256_hex)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as out, open(src, "rb") as source:
        out.write(header)
        while chunk := source.read(_CHUNK):
            out.write(_xor(chunk))
        out.flush()
        os.fsync(out.fileno())


def decode_file(src: str | os.PathLike, dest: str | os.PathLike) -> None:
    """変換した隔離ファイル src を元に戻して dest に書く。大きさ・SHA-256 が記録と一致しなければ
    QuarantineFormatError（dest は消す）"""
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as source:
            header = source.read(HEADER_SIZE)
            if len(header) != HEADER_SIZE or not header.startswith(MAGIC) or header[4] != VERSION:
                raise QuarantineFormatError("隔離ファイルの形式が正しくありません")
            size = int.from_bytes(header[5:13], "big")
            expected = header[13:].hex()
            digest = hashlib.sha256()
            written = 0
            while chunk := source.read(_CHUNK):
                plain = _xor(chunk)
                digest.update(plain)
                written += len(plain)
                out.write(plain)
            out.flush()
            os.fsync(out.fileno())
        if written != size or digest.hexdigest() != expected:
            raise QuarantineFormatError("隔離ファイルの中身が記録と一致しません")
    except BaseException:
        try:
            os.remove(dest)
        except OSError:
            pass
        raise


def read_header(src: str | os.PathLike) -> tuple[int, str] | None:
    """変換した隔離ファイルのヘッダ（元の大きさ, SHA-256）。形式が違えば None"""
    with open(src, "rb") as source:
        header = source.read(HEADER_SIZE)
    if len(header) != HEADER_SIZE or not header.startswith(MAGIC) or header[4] != VERSION:
        return None
    return int.from_bytes(header[5:13], "big"), header[13:].hex()
