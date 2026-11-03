"""
トレイGUI の通知（サービス化・Web API設計書 7.2節。段階 12-4b）

サービスはデスクトップに通知を出せないため、通知を未読として保存する（core/notification_store.py）。
トレイGUI はそれを読んで、この利用者のデスクトップに出す。
- 起動時・つなぎ直したとき: GET /notifications/pending。まだ出していない通知が 2 件以上なら、まとめて 1 件にする
- notification イベント: 1 件ずつ出す
- 既読（トーストのクリックで開いた画面の通知・通知の一覧の「既読にする」）: POST /notifications/ack
- notification.acked イベント（ほかのトレイGUI・WebUI で既読にしたものも届く）: 通知センターに残っているものを消す
  （Windows: Toast の Tag と History.Remove、Linux: notify-send --print-id の番号と CloseNotification）
出した通知の ID はメモリだけに持つ（起動し直したら、未読をもう一度まとめて出す）。

通知を出す・消す処理（PowerShell・notify-send）は時間がかかるため、1 本の別のスレッドで順に行う。
"""
from __future__ import annotations

import concurrent.futures
import logging
import sys
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, Signal

from core.exceptions import AppError, DesktopNotifyError

logger = logging.getLogger("clamav_frontend")

#: まとめた通知の本文に並べる件数
_SUMMARY_LINES = 2
#: まとめた通知の 1 行の長さ
_SUMMARY_WIDTH = 80


@dataclass
class _Shown:
    """デスクトップに出した 1 件（まとめた通知は、複数の通知の ID を持つ）"""

    ids: set[int]
    handle: concurrent.futures.Future = field(repr=False)


def _summary_line(item: dict) -> str:
    title = item.get("title", "")
    first = (item.get("body") or "").split("\n", 1)[0]
    line = f"{title}: {first}" if first else title
    return line if len(line) <= _SUMMARY_WIDTH else line[:_SUMMARY_WIDTH - 1] + "…"


class DesktopToaster:
    """この利用者のデスクトップに通知を出す・消す（別のスレッドで順に行う）"""

    def __init__(self, icon_path: str | None = None, system: str | None = None) -> None:
        self._icon_path = icon_path
        self._windows = (system or sys.platform) in ("win32", "Windows")
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="toast")

    def show(self, title: str, body: str, open_screen: str | None, tag: str) -> concurrent.futures.Future:
        return self._executor.submit(self._show, title, body, open_screen, tag)

    def close(self, handle: concurrent.futures.Future) -> None:
        self._executor.submit(self._close, handle)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _show(self, title: str, body: str, open_screen: str | None, tag: str):
        from core.notification_service import show_linux_notification, show_windows_toast

        try:
            if self._windows:
                show_windows_toast(title, body, open_screen, tag=tag)
                return tag
            return show_linux_notification(title, body, self._icon_path, print_id=True)
        except DesktopNotifyError as exc:
            logger.warning(f"デスクトップ通知の表示に失敗しました (detail: {exc.detail})", extra={"error_code": exc.code})
            return None

    def _close(self, handle: concurrent.futures.Future) -> None:
        from core.notification_service import close_linux_notification, remove_windows_toast

        target = handle.result()  # 同じスレッドで先に出しているため、待たずに終わっている
        if target is None:
            return
        try:
            if self._windows:
                remove_windows_toast(target)
            else:
                close_linux_notification(target)
        except DesktopNotifyError as exc:
            logger.warning(f"既読にした通知を閉じられませんでした (detail: {exc.detail})", extra={"error_code": exc.code})


class TrayNotifier(QObject):
    """サービスの未読の通知を、デスクトップに出し、既読にする"""

    #: 未読の件数が変わった
    pending_changed = Signal(int)

    def __init__(self, api, events, toaster: DesktopToaster, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._api = api
        self._toaster = toaster
        self._pending: dict[int, dict] = {}
        #: 出した通知の ID（既読になっても残す。同じ通知を二度出さない）
        self._seen: set[int] = set()
        self._shown: list[_Shown] = []
        events.event.connect(self._on_event)

    @property
    def pending(self) -> list[dict]:
        """未読の通知（新しい順）"""
        return sorted(self._pending.values(), key=lambda n: n["id"], reverse=True)

    def sync_pending(self) -> None:
        """未読をまとめて読み、まだ出していないものを出す（起動時・サービスにつなぎ直したとき）"""
        try:
            data = self._api.call("GET", "/notifications/pending")
        except AppError as exc:
            logger.warning(f"未読の通知を読めませんでした ({exc.code}: {exc.detail or exc.message})")
            return
        items = [n for n in data.get("notifications", []) if isinstance(n, dict) and isinstance(n.get("id"), int)]
        self._pending = {n["id"]: n for n in items}
        self.pending_changed.emit(len(self._pending))
        new = [n for n in self.pending if n["id"] not in self._seen]
        if len(new) == 1:
            self._present(new)
        elif new:
            self._present(new, total=int(data.get("total", len(items))))

    def ack(self, ids: list[int] | None = None) -> None:
        """既読にする（None はすべて）。エラーは AppError を送出する"""
        if ids is not None and not ids:
            return
        result = self._api.call("POST", "/notifications/ack", {"all": True} if ids is None else {"ids": list(ids)})
        # notification.acked イベントでも届くが、イベントが遅れても一覧・通知センターに残らないよう、ここでも反映する
        self._on_acked(result.get("acked", []) if isinstance(result, dict) else [])

    def ack_screen(self, screen: str) -> None:
        """トーストのクリックで開いた画面の通知を既読にする"""
        ids = [n["id"] for n in self._pending.values() if n.get("open_screen") == screen]
        try:
            self.ack(ids)
        except AppError as exc:
            logger.warning(f"通知を既読にできませんでした ({exc.code}: {exc.detail or exc.message})")

    def shutdown(self) -> None:
        self._toaster.shutdown()

    # -- 内部 ------------------------------------------------------------------------------------
    def _on_event(self, name: str, data) -> None:
        if name == "notification" and isinstance(data, dict) and isinstance(data.get("id"), int):
            self._pending[data["id"]] = data
            self.pending_changed.emit(len(self._pending))
            if data["id"] not in self._seen:
                self._present([data])
        elif name == "notification.acked" and isinstance(data, dict):
            self._on_acked(data.get("ids", []))

    def _present(self, items: list[dict], total: int | None = None) -> None:
        ids = {n["id"] for n in items}
        self._seen |= ids
        if total is None:
            item = items[0]
            title, body, open_screen, tag = item.get("title", ""), item.get("body", ""), item.get("open_screen"), \
                f"n{item['id']}"
        else:
            title = f"未読の通知が {total} 件あります"
            # 同じ種類の通知（ジョブの失敗など）でも見分けられるよう、本文の最初の行を添える
            lines = [_summary_line(n) for n in items[:_SUMMARY_LINES]]
            if total > _SUMMARY_LINES:
                lines.append(f"他{total - _SUMMARY_LINES}件")
            body = "\n".join(lines)
            screens = {n.get("open_screen") for n in items}
            open_screen = screens.pop() if len(screens) == 1 else None
            tag = f"s{max(ids)}"
        self._shown.append(_Shown(ids, self._toaster.show(title, body, open_screen, tag)))

    def _on_acked(self, ids) -> None:
        acked = {i for i in ids if isinstance(i, int)}
        if not acked:
            return
        before = len(self._pending)
        for i in acked:
            self._pending.pop(i, None)
        if len(self._pending) != before:
            self.pending_changed.emit(len(self._pending))
        remaining = []
        for shown in self._shown:
            shown.ids -= acked
            if shown.ids:
                remaining.append(shown)
            else:
                self._toaster.close(shown.handle)
        self._shown = remaining
