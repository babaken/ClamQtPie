"""
外部から来る文字列（ファイル名・ウイルス名等）を、ログや通知に安全に書くための整形

Linuxのファイル名には改行などの制御文字も使える。そのままログに書くと1件の記録が複数の行に分かれ、
別の記録のように見えてしまう（ログ画面もそれを別の行として読む）。通知・メールの本文でも同じことが起きる。
そのため、制御文字を「\\n」「\\x1b」のような見える形に置き換える。バックスラッシュ自体は置き換えない
（Windowsのパスがそのまま読めるように。置き換えた結果をもう一度通しても変わらない）。
"""
from __future__ import annotations

import re

# 制御文字と、対になっていないサロゲート（UTF-8 にできない名前。そのままではファイル・JSON に書けない）
_CONTROL_RE = re.compile("[\x00-\x1f\x7f\u2028\u2029\ud800-\udfff]")
_NAMED = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _escape(match: re.Match) -> str:
    char = match.group()
    if char in _NAMED:
        return _NAMED[char]
    code = ord(char)
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"


def visible_controls(text: str) -> str:
    """制御文字（U+0000〜U+001F、U+007F、U+2028、U+2029）と、対になっていないサロゲート（U+D800〜U+DFFF。
    Linux の UTF-8 として正しくないファイル名など）を見える形に置き換える"""
    return _CONTROL_RE.sub(_escape, text)
