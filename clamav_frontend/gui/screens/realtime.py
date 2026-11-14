"""
リアルタイム画面

参照: 画面設計書3章
Phase5: RealtimeManager / WatchTargetService / ExcludeService との結線を追加
段階 12-4c（トレイGUI）: 監視対象・除外・検知時アクションの変更を画面の中にためておき、［変更を保存］で
          1 回の昇格にまとめる（bind_services の batch。gui/pending_edits.py）
段階 12-4e: 従来のアプリ（その場でサービスを変える）の経路を除いた。監視中の反映はサービスが行う
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from api_client.admin_ops import Operation
from core.exceptions import AppError, WatchTargetLostError
from gui.error_dialog import show_confirm_dialog, show_error_dialog
from gui.pending_edits import PendingEdits, SaveBar, failure_text
from gui.screens.base import BaseScreen, configure_table, wrap_layout

_ID_ROLE = 0x0102

#: 監視するフォルダの一覧で、外す予定の行の表示（ボタン・確認画面の「外す」とそろえる）
_REMOVED_LABEL = "外す"
#: ［フォルダを外す］の表示（選んだ行が外す予定・保存前の追加のときは、押すと何が起きるかを示す）
_REMOVE_TARGET_TEXT = "フォルダを外す"
_UNDO_REMOVE_TARGET_TEXT = "外すのをやめる"
_CANCEL_ADD_TARGET_TEXT = "追加をやめる"

#: 除外タブの表示ラベルとExcludeService上のtype値の対応(画面設計書3章)
_EXCLUDE_TYPES = [
    ("ファイル", "file"),
    ("フォルダ", "folder"),
    ("拡張子", "extension"),
    ("プロセス", "process"),
]


class RealtimeScreen(BaseScreen):
    screen_title = "リアルタイム ファイル保護"
    screen_subtitle = "監視するフォルダの中でファイルが作成・変更されるとスキャンします。"

    def build_content(self) -> None:
        # 1. 監視状態トグル
        self.begin_card("リアルタイム フォルダ監視", "指定したフォルダを監視し、作成・変更されたファイルをバックグラウンドでスキャンします。")
        toggle_layout = QHBoxLayout()
        self.toggle_button = QPushButton("監視を開始する")
        self.toggle_button.setCheckable(True)
        self.toggle_button.clicked.connect(self._on_toggle_clicked)
        self.mode_label = QLabel("方式: -")
        toggle_layout.addWidget(self.toggle_button)
        toggle_layout.addWidget(self.mode_label)
        self._add_hlayout(toggle_layout)

        # 2. 監視対象パス一覧
        self.begin_card("監視するフォルダ")
        self.target_table = QTableWidget(0, 2)
        self.target_table.setHorizontalHeaderLabels(["パス", "有効"])
        self.add_layout_widget(self.target_table)
        target_action_layout = QHBoxLayout()
        self.add_target_button = QPushButton("フォルダを追加")
        self.remove_target_button = QPushButton(_REMOVE_TARGET_TEXT)
        self.add_target_button.clicked.connect(self._on_add_target_clicked)
        self.remove_target_button.clicked.connect(self._on_remove_target_clicked)
        self.toggle_target_button = QPushButton("有効/無効を切替")
        self.toggle_target_button.clicked.connect(self._on_toggle_target_clicked)
        self.target_table.itemSelectionChanged.connect(self._update_remove_target_button)
        target_action_layout.addWidget(self.add_target_button)
        target_action_layout.addWidget(self.remove_target_button)
        target_action_layout.addWidget(self.toggle_target_button)
        self._add_hlayout(target_action_layout)
        self.no_target_hint_label = QLabel(
            "監視するフォルダがありません。「フォルダを追加」から監視するフォルダを追加してください。"
        )
        self.no_target_hint_label.setObjectName("cardText")
        self.no_target_hint_label.setWordWrap(True)
        self.add_layout_widget(self.no_target_hint_label)

        # 3. 除外設定タブ
        self.begin_card("除外")
        self.exclude_tabs = QTabWidget()
        self._exclude_tables: dict[str, QTableWidget] = {}
        for label, type_key in _EXCLUDE_TYPES:
            tab = QWidget()
            tab_layout = QVBoxLayout(tab)
            table = QTableWidget(0, 2)
            table.setHorizontalHeaderLabels(["値", "有効"])
            configure_table(table)
            tab_layout.addWidget(table)
            self.exclude_tabs.addTab(tab, label)
            self._exclude_tables[type_key] = table
        self.add_layout_widget(self.exclude_tabs)

        # 画面設計書3章: 各区分ごとに追加（入力 or 参照）／削除／有効-無効トグル
        exclude_action_layout = QHBoxLayout()
        self.add_exclude_button = QPushButton("除外を追加")
        self.remove_exclude_button = QPushButton("除外を削除")
        self.toggle_exclude_button = QPushButton("除外の有効/無効を切替")
        self.add_exclude_button.clicked.connect(self._on_add_exclude_clicked)
        self.remove_exclude_button.clicked.connect(self._on_remove_exclude_clicked)
        self.toggle_exclude_button.clicked.connect(self._on_toggle_exclude_clicked)
        for btn in (self.add_exclude_button, self.remove_exclude_button, self.toggle_exclude_button):
            exclude_action_layout.addWidget(btn)
        self._add_hlayout(exclude_action_layout)

        # 4. 検知時アクション設定
        self.begin_card("検知時アクション")
        self.action_quarantine_radio = QRadioButton("自動隔離")
        self.action_notify_only_radio = QRadioButton("通知のみ（手動対応）")
        self.action_quarantine_radio.setChecked(True)
        self.action_quarantine_radio.toggled.connect(self._on_action_changed)
        self.add_layout_widget(self.action_quarantine_radio)
        self.add_layout_widget(self.action_notify_only_radio)

        # 5. 変更の保存
        self.save_card = self.begin_card("変更の保存")
        self.save_bar = SaveBar(self.save_changes, self.discard_changes)
        self._add_hlayout(self.save_bar.layout)

        self._batch = None
        self._config_operation = None
        self._pending_targets = PendingEdits()
        self._pending_excludes = PendingEdits()
        self._pending_action: str | None = None
        self._syncing_action = False
        self._realtime_manager = None
        self._watch_target_service = None
        self._exclude_service = None
        self._first_show_guided = False
        self._showing_scan_error = False

    # -- サービス層との結線 -----------------------------------------------------
    def bind_services(self, realtime_manager, watch_target_service, exclude_service, batch,
                      config_operation=None) -> None:
        """batch(操作の一覧): ためておいた変更を、［変更を保存］でまとめて送る（api_client.elevation）。
        config_operation(検知時アクション) -> Operation | None: 検知時アクションを保存する操作（PUT /config）"""
        self._batch = batch
        self._config_operation = config_operation
        self._realtime_manager = realtime_manager
        self._watch_target_service = watch_target_service
        self._exclude_service = exclude_service

        self._realtime_manager.detected.connect(self._on_detected)
        self._realtime_manager.watch_target_lost.connect(self._on_watch_target_lost)
        self._realtime_manager.scan_error.connect(self._on_scan_error)
        self.mode_label.setText(f"方式: {getattr(self._realtime_manager, 'mode_name', '-')}")

        self.sync_action_from_config()
        self.reload_targets()
        self.reload_excludes()

    def sync_action_from_config(self) -> None:
        """検知時アクションのラジオボタンを設定値に合わせる（設定画面で既定値を保存した直後にも呼ばれる）"""
        if self._realtime_manager is None:
            return
        if self._pending_action is not None:
            return  # 保存していない選択を、ほかの画面での保存で上書きしない
        action = self._realtime_manager.config.default_action
        self._syncing_action = True
        try:
            self.action_quarantine_radio.setChecked(action == "quarantine")
            self.action_notify_only_radio.setChecked(action == "notify_only")
        finally:
            self._syncing_action = False

    # -- 監視対象パス -----------------------------------------------------
    def reload_targets(self) -> None:
        if self._watch_target_service is None:
            return
        records = self._watch_target_service.list_all()
        pending = self._pending_targets
        pending.prune({r.id for r in records})
        rows = [(r.id, r.path, pending.state_text(r.id, r.enabled, _REMOVED_LABEL)) for r in records]
        rows += [(-(i + 1), item["path"], pending.state_text(-(i + 1), True)) for i, item in enumerate(pending.added)]
        self.target_table.setRowCount(0)
        for key, path, state in rows:
            row = self.target_table.rowCount()
            self.target_table.insertRow(row)
            path_item = QTableWidgetItem(path)
            path_item.setData(_ID_ROLE, key)
            self.target_table.setItem(row, 0, path_item)
            self.target_table.setItem(row, 1, QTableWidgetItem(state))
        self.no_target_hint_label.setVisible(len(rows) == 0)
        self._update_remove_target_button()
        self._show_pending_count()
        # Windowsリアルタイム監視詳細設計書3.1節: 対象0件の間はON操作自体を無効化し、ツールチップで理由を表示
        has_enabled = any(r.enabled for r in records)
        running = self._realtime_manager is not None and self._realtime_manager.is_running
        self.toggle_button.setEnabled(has_enabled or running)
        self.toggle_button.setToolTip(
            "" if has_enabled or running else "監視するフォルダを追加する（有効にする）と開始できます"
        )

    def sync_toggle_state(self, running: bool) -> None:
        """ホーム画面・トレイから監視を開始/停止した場合も表示を合わせる（トレイGUI の定期更新から呼ばれる）"""
        if self.toggle_button.isChecked() != running:
            self.toggle_button.setChecked(running)
            self.toggle_button.setText("監視を停止する" if running else "監視を開始する")
            self.reload_targets()

    def showEvent(self, event) -> None:  # noqa: N802 - Qtのオーバーライド
        super().showEvent(event)
        # Windowsリアルタイム監視詳細設計書3.1節: 監視対象が未設定なら初回表示時に案内する（起動ごとに1回）
        if (
            not self._first_show_guided
            and self._watch_target_service is not None
            and not self._watch_target_service.list_all()
        ):
            self._first_show_guided = True
            if _ask_add_target_now(self):
                self._on_add_target_clicked()

    def _on_toggle_target_clicked(self) -> None:
        if self._watch_target_service is None:
            return
        rows = {idx.row() for idx in self.target_table.selectedIndexes()}
        if not rows:
            return
        target_id = self.target_table.item(next(iter(rows)), 0).data(_ID_ROLE)
        saved = next((r.enabled for r in self._watch_target_service.list_all() if r.id == target_id), True)
        self._pending_targets.toggle(target_id, saved)
        self.reload_targets()

    def _on_add_target_clicked(self) -> None:
        if self._watch_target_service is None:
            return
        path = QFileDialog.getExistingDirectory(self, "監視するフォルダを選択")
        if not path:
            return
        self._pending_targets.add({"path": path})
        self.reload_targets()

    def _selected_target_id(self) -> int | None:
        rows = {idx.row() for idx in self.target_table.selectedIndexes()}
        return self.target_table.item(next(iter(rows)), 0).data(_ID_ROLE) if rows else None

    def _update_remove_target_button(self) -> None:
        """選んだ行が外す予定なら［外すのをやめる］、保存前の追加なら［追加をやめる］と出す"""
        key = self._selected_target_id()
        if key is not None and key < 0:
            text = _CANCEL_ADD_TARGET_TEXT
        elif key is not None and key in self._pending_targets.removed:
            text = _UNDO_REMOVE_TARGET_TEXT
        else:
            text = _REMOVE_TARGET_TEXT
        self.remove_target_button.setText(text)

    def _on_remove_target_clicked(self) -> None:
        if self._watch_target_service is None:
            return
        key = self._selected_target_id()
        if key is None:
            return
        self._pending_targets.remove(key)
        self.reload_targets()

    # -- 除外設定 -----------------------------------------------------
    def reload_excludes(self) -> None:
        if self._exclude_service is None:
            return
        all_rules = self._exclude_service.list_all()
        pending = self._pending_excludes
        pending.prune({r.id for r in all_rules})
        for type_key, table in self._exclude_tables.items():
            rows = [(r.id, r.value, pending.state_text(r.id, r.enabled)) for r in all_rules if r.type == type_key]
            rows += [(-(i + 1), item["value"], pending.state_text(-(i + 1), True))
                     for i, item in enumerate(pending.added) if item["type"] == type_key]
            table.setRowCount(0)
            for key, value, state in rows:
                row = table.rowCount()
                table.insertRow(row)
                value_item = QTableWidgetItem(value)
                value_item.setData(_ID_ROLE, key)
                table.setItem(row, 0, value_item)
                table.setItem(row, 1, QTableWidgetItem(state))
        self._show_pending_count()

    def _current_exclude_type(self) -> str:
        return _EXCLUDE_TYPES[self.exclude_tabs.currentIndex()][1]

    def _selected_exclude_id(self) -> int | None:
        table = self._exclude_tables[self._current_exclude_type()]
        rows = {idx.row() for idx in table.selectedIndexes()}
        if not rows:
            return None
        item = table.item(next(iter(rows)), 0)
        return item.data(_ID_ROLE) if item else None

    def _ask_exclude_value(self, type_key: str) -> str:
        """除外区分ごとの入力方法（ファイル/フォルダは参照ダイアログ、拡張子/プロセスはテキスト入力）"""
        if type_key == "file":
            path, _ = QFileDialog.getOpenFileName(self, "除外するファイルを選択")
            return path
        if type_key == "folder":
            return QFileDialog.getExistingDirectory(self, "除外するフォルダを選択")
        if type_key == "extension":
            text, ok = QInputDialog.getText(self, "拡張子の除外", "除外する拡張子（例: .log）")
            if not ok or not text.strip():
                return ""
            ext = text.strip().lower()
            return ext if ext.startswith(".") else f".{ext}"
        text, ok = QInputDialog.getText(self, "プロセスの除外", "除外するプロセス名（例: notepad.exe）")
        return text.strip() if ok else ""

    def _on_add_exclude_clicked(self) -> None:
        if self._exclude_service is None:
            return
        type_key = self._current_exclude_type()
        value = self._ask_exclude_value(type_key)
        if not value:
            return
        existing = {(r.type, r.value) for r in self._exclude_service.list_all()}
        existing |= {(item["type"], item["value"]) for item in self._pending_excludes.added}
        if (type_key, value) not in existing:  # 同じ値の重複登録はしない
            self._pending_excludes.add({"type": type_key, "value": value})
        self.reload_excludes()

    def _on_remove_exclude_clicked(self) -> None:
        if self._exclude_service is None:
            return
        exclude_id = self._selected_exclude_id()
        if exclude_id is None:
            return
        self._pending_excludes.remove(exclude_id)  # 保存するまでは取り消せるため、ここでは確かめない
        self.reload_excludes()

    def _on_toggle_exclude_clicked(self) -> None:
        if self._exclude_service is None:
            return
        exclude_id = self._selected_exclude_id()
        if exclude_id is None:
            return
        saved = next((r.enabled for r in self._exclude_service.list_all() if r.id == exclude_id), True)
        self._pending_excludes.toggle(exclude_id, saved)
        self.reload_excludes()

    # -- 監視トグル -----------------------------------------------------
    def _on_toggle_clicked(self, checked: bool) -> None:
        if self._realtime_manager is None:
            self.toggle_button.setChecked(False)
            return

        if checked:
            if not self._watch_target_service.list_enabled_paths():
                # 先に案内する（サービスに送ると、昇格の認証と確認画面を通った後で断られる）
                self.toggle_button.setChecked(False)
                show_no_target_warning(self)
                return
            started = self._realtime_manager.start()
            if not started:
                self.toggle_button.setChecked(False)
                # clamonacc方式では対象があっても起動失敗し得る（その場合はscan_errorで別途表示済み）
                if not self._watch_target_service.list_enabled_paths():
                    show_no_target_warning(self)
                return
            self.toggle_button.setText("監視を停止する")
        else:
            if not show_confirm_dialog(self, "監視停止の確認", "リアルタイム監視を停止しますか？"):
                self.toggle_button.setChecked(True)
                return
            self._realtime_manager.stop()
            self.toggle_button.setText("監視を開始する")

    def _on_action_changed(self, checked: bool) -> None:
        if self._realtime_manager is None or self._syncing_action:
            return
        action = "quarantine" if checked else "notify_only"
        saved = self._realtime_manager.config.default_action
        self._pending_action = None if action == saved else action
        self._show_pending_count()

    # -- 変更の保存 ----------------------------------------------------------------
    def has_unsaved_changes(self) -> bool:
        return self._pending_count() > 0

    def _pending_count(self) -> int:
        return len(self._pending_targets) + len(self._pending_excludes) + (self._pending_action is not None)

    def _show_pending_count(self) -> None:
        self.save_bar.show_count(self._pending_count())

    def pending_operations(self) -> list[Operation]:
        """保存していない変更を、API の操作の一覧にする（削除 → 有効無効 → 追加 → 検知時アクションの順）"""
        lists = (("/excludes", self._pending_excludes), ("/realtime/targets", self._pending_targets))
        ops = [Operation("DELETE", f"{path}/{key}") for path, pending in lists for key in sorted(pending.removed)]
        ops += [Operation("PATCH", f"{path}/{key}", {"enabled": enabled})
                for path, pending in lists for key, enabled in sorted(pending.enabled.items())]
        ops += [Operation("POST", "/realtime/targets", {"path": item["path"]}) for item in self._pending_targets.added]
        ops += [Operation("POST", "/excludes", {"type": item["type"], "value": item["value"],
                                                "enabled": item.get("enabled", True)})
                for item in self._pending_excludes.added]
        if self._pending_action is not None and self._config_operation is not None:
            op = self._config_operation(self._pending_action)
            if op is not None:
                ops.append(op)
        return ops

    def save_changes(self) -> bool:
        """［変更を保存］: 1 回の昇格でまとめて保存する。保存できた（または変更が無い）とき True"""
        if self._batch is None:
            return True  # まだ結線していない
        ops = self.pending_operations()
        if not ops:
            self.discard_changes()
            return True
        try:
            self._batch(ops)
        except AppError as exc:
            from core.exceptions import ElevationCancelledError

            if isinstance(exc, ElevationCancelledError):
                return False  # 取り消した: 変更はそのまま残す
            self._clear_pending()
            self._reload_all()
            self.save_bar.show_result(failure_text(exc, len(ops)) + "一覧はサービスの今の内容です。", ok=False)
            show_error_dialog(self, exc)
            return False
        self._clear_pending()
        self._reload_all()
        self.save_bar.show_result(f"{len(ops)} 件の変更を保存しました。", ok=True)
        return True

    def discard_changes(self) -> None:
        self._clear_pending()
        self._reload_all()
        self.save_bar.show_result("保存していない変更を取り消しました。", ok=True)

    def _clear_pending(self) -> None:
        self._pending_targets.clear()
        self._pending_excludes.clear()
        self._pending_action = None

    def _reload_all(self) -> None:
        self.reload_targets()
        self.reload_excludes()
        self.sync_action_from_config()

    # -- RealtimeManagerからのシグナル受信 -----------------------------------
    def _on_detected(self, path: str, virus_name: str) -> None:
        # Phase7で通知連携するまでは何もしない（検知〜隔離自体はRealtimeManager内で完結）
        pass

    def _on_watch_target_lost(self, path: str) -> None:
        # エラーコード一覧E2001: 該当パスの監視停止・無効化（マネージャ側で実施済み）＋GUI通知
        self.reload_targets()
        show_error_dialog(self, WatchTargetLostError(detail=path))

    def _on_scan_error(self, error) -> None:
        # clamonaccの異常終了等で監視自体が止まった場合はトグル表示を戻す
        if self.toggle_button.isChecked() and not self._realtime_manager.is_running:
            self.toggle_button.setChecked(False)
            self.toggle_button.setText("監視を開始する")
        # エラーダイアログを表示している間に届いたエラーは重ねて表示しない（ログには記録済み）。
        # ダイアログのイベントループ中に次のエラーが届き、ダイアログが入れ子で積み重なっていた（コードレビューで修正）
        if self._showing_scan_error:
            return
        self._showing_scan_error = True
        try:
            show_error_dialog(self, error)
        finally:
            self._showing_scan_error = False

    def _add_hlayout(self, layout: QHBoxLayout) -> None:
        self.add_layout_widget(wrap_layout(layout))


def _ask_add_target_now(parent) -> bool:
    """初回案内ダイアログ。「今すぐ追加」ならTrue、「後で設定する」ならFalse"""
    from PySide6.QtWidgets import QMessageBox

    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle("リアルタイム監視")
    box.setText(
        "監視するフォルダがありません。"
        "監視を開始するには、監視するフォルダを追加してください。"
    )
    add_button = box.addButton("今すぐ追加", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("後で設定する", QMessageBox.ButtonRole.RejectRole)
    box.exec()
    return box.clickedButton() is add_button


def show_no_target_warning(parent) -> None:
    from PySide6.QtWidgets import QMessageBox

    QMessageBox.warning(
        parent,
        "監視を開始できません",
        "監視するフォルダがありません。先に、リアルタイム保護の画面の「フォルダを追加」から監視するフォルダを追加してください。",
    )
