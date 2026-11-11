"""
画面の中にためておく変更（トレイGUI。サービス化・Web API設計書 3.2節。段階 12-4c）

除外・監視対象・スケジュールの追加・変更・削除・有効無効の切り替えは、admin の操作のため昇格の確認が要る。
1 つずつ確認を出さないよう、画面の中にためておき、［変更を保存］で 1 回の昇格にまとめる。
- 一覧の ID が正の数の行はサービスにあるもの、負の数（-1, -2, ...）はまだ保存していない追加
- 保存していない変更が残っている画面を離れる・閉じるときは、MainWindow が確認する（has_unsaved_changes）
"""
from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton

from gui.plain_text import plain
from gui.theme import COLOR_NG, COLOR_OK, COLOR_WARN, set_variant

#: 一覧の「状態」に添える印
MARK_UNSAVED = "（保存前）"


@dataclass
class PendingEdits:
    """1 つの一覧の、保存していない変更"""

    added: list[dict] = field(default_factory=list)
    removed: set[int] = field(default_factory=set)
    enabled: dict[int, bool] = field(default_factory=dict)
    updated: dict[int, dict] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.added) + len(self.removed) + len(self.enabled) + len(self.updated)

    def clear(self) -> None:
        self.added.clear()
        self.removed.clear()
        self.enabled.clear()
        self.updated.clear()

    def add(self, values: dict) -> None:
        self.added.append(values)

    def remove(self, key: int) -> None:
        """削除する（保存していない追加は取り消す。削除の予定の行をもう一度選べば、削除をやめる）"""
        if key < 0:
            del self.added[-key - 1]
        elif key in self.removed:
            self.removed.discard(key)
        else:
            self.removed.add(key)
            self.enabled.pop(key, None)
            self.updated.pop(key, None)

    def toggle(self, key: int, saved_enabled: bool) -> None:
        """有効・無効を切り替える（2 回切り替えたら変更なしに戻す）"""
        if key < 0:
            self.added[-key - 1]["enabled"] = not self.added[-key - 1].get("enabled", True)
        elif key in self.enabled:
            del self.enabled[key]
        else:
            self.enabled[key] = not saved_enabled

    def update(self, key: int, values: dict) -> None:
        if key < 0:
            self.added[-key - 1].update(values)
        else:
            self.updated[key] = values

    def prune(self, existing: set[int]) -> None:
        """サービスに無くなった行（WebUI・ほかのトレイGUI が消した）への変更を捨てる（保存すると E8109 になるため）"""
        self.removed &= existing
        for changes in (self.enabled, self.updated):
            for key in [k for k in changes if k not in existing]:
                del changes[key]

    def state_text(self, key: int, saved_enabled: bool, removed_label: str = "削除") -> str:
        """一覧の「有効」の列の表示（removed_label: 削除の予定の行の呼び方。監視するフォルダは「外す」）"""
        if key < 0:
            return "追加" + MARK_UNSAVED
        if key in self.removed:
            return removed_label + MARK_UNSAVED
        enabled = self.enabled.get(key, saved_enabled)
        text = "有効" if enabled else "無効"
        return text + MARK_UNSAVED if key in self.enabled or key in self.updated else text


class SaveBar:
    """［変更を保存］［変更を取り消す］と、保存していない変更の件数・保存の結果の表示"""

    def __init__(self, on_save, on_discard) -> None:
        self.layout = QHBoxLayout()
        self.label = plain(QLabel(""))
        self.label.setWordWrap(True)
        self.save_button = set_variant(QPushButton("変更を保存"), "primary")
        self.discard_button = QPushButton("変更を取り消す")
        self.save_button.clicked.connect(on_save)
        self.discard_button.clicked.connect(on_discard)
        self.layout.addWidget(self.label, 1)
        self.layout.addWidget(self.discard_button)
        self.layout.addWidget(self.save_button)
        self.show_count(0)

    def show_count(self, count: int) -> None:
        self.save_button.setEnabled(count > 0)
        self.discard_button.setEnabled(count > 0)
        if count:
            self.label.setText(f"保存していない変更が {count} 件あります。［変更を保存］で、管理者の権限で保存します。")
            self.label.setStyleSheet(f"color: {COLOR_WARN};")

    def show_result(self, text: str, ok: bool) -> None:
        self.label.setText(text)
        self.label.setStyleSheet(f"color: {COLOR_OK if ok else COLOR_NG};")


def failure_text(error, total: int) -> str:
    """保存の途中で失敗したときの表示（昇格ヘルパーは失敗したところで止まる）"""
    completed = getattr(error, "completed", 0)
    if completed:
        return f"{total} 件のうち {completed} 件を保存したところで失敗しました。残りは保存していません。"
    return "保存できませんでした。"
