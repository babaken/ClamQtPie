"""
Python 3.15 の遅延 import（PEP 810）への対策

concurrent.futures は ThreadPoolExecutor・ProcessPoolExecutor を `lazy from .thread import ...` で読み込む。
PySide6 を読み込んだ後では、`concurrent.futures.ThreadPoolExecutor` が遅延 import の印（lazy_import）のまま返り、
APScheduler の起動時に "TypeError: 'lazy_import' object is not callable" で落ちる。

以前は PySide6 より前に属性を読んで実体化していたが、配布版（PyInstaller）では PySide6 の runtime hook が
本体より先に PySide6 を読み込むため、その順番に頼れず、起動の直後に落ちていた。
そのため、読み込みの順番に関係なく効くよう、下位のモジュールから本物のクラスを読み込んで名前を置き換える。
"""
from __future__ import annotations

import concurrent.futures


def reify_concurrent_futures() -> None:
    """app.py・tests/conftest.py の最初に呼ぶ（何度呼んでもよい）"""
    from concurrent.futures.process import ProcessPoolExecutor
    from concurrent.futures.thread import ThreadPoolExecutor

    concurrent.futures.ThreadPoolExecutor = ThreadPoolExecutor
    concurrent.futures.ProcessPoolExecutor = ProcessPoolExecutor
