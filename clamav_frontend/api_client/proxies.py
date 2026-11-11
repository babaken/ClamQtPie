"""
画面に渡すサービスのプロキシ（サービス化・Web API設計書 10章。段階 12-4b）

各画面の bind_services(...) に、これまでのサービスの代わりに渡す。メソッドの名前・引数・戻り値の形を、
これまでのサービスにそろえ、中で内部 API を呼ぶ（api_client/sync.py）。シグナル（スキャンの進み具合・
リアルタイム監視の検知など）は、サービスのイベント（EventStream）から作る。

admin の操作（隔離の復元・除外の変更など）も同じ形で API を呼ぶ。user のトークンでは 403（E8102）になるため、
段階 12-4c で昇格ヘルパーを通すように変える。
"""
from __future__ import annotations

import logging
import time
from dataclasses import fields
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QObject, Signal

from api_client.events import EventStream
from api_client.sync import SyncCaller
from core.config_model import ConfigModel
from core.gui_config import gui_config_path, read_gui_general, save_gui_general
from core.exceptions import AppError
from core.log_reader_service import LogEntry, LogReaderService
from core.scheduler_service import ScheduleRecord
from core.update_service import UpdateHistoryRecord
from core.watch_target_service import WatchTargetRecord

logger = logging.getLogger("clamav_frontend")

#: GET /status の結果を使い回す秒数（ホーム画面・ステータスバーなどが続けて読むため）
STATUS_CACHE_SEC = 2.0


def _from_dict(cls, data: dict):
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


class StatusCache:
    """GET /status を少しの間使い回す"""

    def __init__(self, api: SyncCaller, clock=time.monotonic) -> None:
        self._api = api
        self._clock = clock
        self._data: dict | None = None
        self._at = float("-inf")

    def get(self) -> dict:
        if self._data is None or self._clock() - self._at >= STATUS_CACHE_SEC:
            self._data = self._api.call("GET", "/status")
            self._at = self._clock()
        return self._data

    def invalidate(self) -> None:
        self._data = None


# -- 設定 --------------------------------------------------------------------------------------
class ConfigProxy:
    """ConfigService の代わり。サービスの設定は GET /config（config.changed で読み直す）、利用者ごとの GUI の設定
    （general: 自動起動・最小化など）は gui.json（core/gui_config.py）"""

    def __init__(self, api: SyncCaller, gui_path: Path | None = None) -> None:
        self._api = api
        self._gui_path = gui_path or gui_config_path()
        self._current: ConfigModel | None = None
        #: 最後に読んだサービスの設定（保存のときに、変えたかどうかを比べる）
        self._service_part: dict | None = None
        self.protected: list[str] = []

    @property
    def current(self) -> ConfigModel:
        if self._current is None:
            self.reload()
        return self._current

    def load(self) -> ConfigModel:
        return self.reload()

    def reload(self) -> ConfigModel:
        data = self._api.call("GET", "/config")
        self.protected = list(data.pop("protected", []))
        data.get("smtp", {}).pop("has_password", None)
        model = ConfigModel.from_dict(data)
        model.general = read_gui_general(self._gui_path)
        self._service_part = _service_part(model)
        self._current = model
        return model

    def invalidate(self) -> None:
        self._current = None

    def operation_for(self, model: ConfigModel):
        """model のサービスの設定を保存する操作（PUT /config）。最後に読んだ内容と同じなら None"""
        from api_client.admin_ops import Operation

        service_part = _service_part(model)
        return None if service_part == self._service_part else Operation("PUT", "/config", service_part)

    def save(self, model: ConfigModel, smtp_password: str | None = None) -> None:
        """GUI の設定は gui.json に書き、サービスの設定は変えたときだけ PUT /config（admin）で保存する。
        SMTP のパスワードも渡されたら、同じ 1 回の昇格で保存する（段階 12-4c）"""
        from api_client.admin_ops import Operation

        save_gui_general(self._gui_path, model.general)
        operations = [op for op in (self.operation_for(model),) if op is not None]
        if smtp_password:
            operations.append(Operation("PUT", "/config/smtp-password", {"password": smtp_password}))
        if operations:
            batch = getattr(self._api, "batch", None)
            if batch is not None:
                batch(operations)
            else:
                for op in operations:
                    self._api.call(op.method, op.path, op.body)
        self._service_part = _service_part(model)
        self._current = model

    def set_smtp_password(self, password: str) -> None:
        self._api.call("PUT", "/config/smtp-password", {"password": password})

    def delete_smtp_password(self) -> None:
        self._api.call("DELETE", "/config/smtp-password")


def _service_part(model: ConfigModel) -> dict:
    """PUT /config に送る部分（API の設定・GUI の設定・版数を除く）"""
    body = model.to_dict()
    for section in ("api", "general", "schema_version"):
        body.pop(section, None)
    return body


# -- 一覧・変更 ----------------------------------------------------------------------------------
class QuarantineProxy:
    def __init__(self, api: SyncCaller) -> None:
        self._api = api

    def list_all(self) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(**{k: v for k, v in item.items() if k != "status"},
                            restored=int(item.get("status") == "restored"),
                            deleted=int(item.get("status") == "deleted"))
            for item in self._api.items("/quarantine")
        ]

    def restore(self, quarantine_id: int, dest_path=None, overwrite: bool = False) -> None:
        if dest_path:
            # 元の場所以外への復元は clamqtpie-ctl quarantine restore --to（6.3節）
            raise AppError("元の場所以外への復元は、管理用コマンドで行ってください",
                           detail=f"clamqtpie-ctl quarantine restore {quarantine_id} --to <場所>")
        self._api.call("POST", f"/quarantine/{quarantine_id}/restore", {"overwrite": overwrite})

    def delete_permanently(self, quarantine_id: int) -> None:
        self._api.call("DELETE", f"/quarantine/{quarantine_id}")

    def delete_many(self, quarantine_ids: list[int]) -> None:
        """選んだ隔離ファイルをまとめて削除する（昇格の確認は 1 回。段階 12-4c）"""
        from api_client.admin_ops import Operation

        operations = [Operation("DELETE", f"/quarantine/{qid}") for qid in quarantine_ids]
        batch = getattr(self._api, "batch", None)
        if batch is not None:
            batch(operations)
            return
        for op in operations:
            self._api.call(op.method, op.path)

    def quarantine(self, original_path, virus_name: str):
        return SimpleNamespace(**self._api.call("POST", "/quarantine",
                                                {"path": str(original_path), "virus_name": virus_name}))


class ExcludeProxy:
    def __init__(self, api: SyncCaller) -> None:
        self._api = api

    def list_all(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(**item) for item in self._api.items("/excludes")]

    def add(self, type_: str, value: str, enabled: bool = True):
        return SimpleNamespace(**self._api.call("POST", "/excludes",
                                                {"type": type_, "value": value, "enabled": enabled}))

    def remove(self, exclude_id: int) -> None:
        self._api.call("DELETE", f"/excludes/{exclude_id}")

    def set_enabled(self, exclude_id: int, enabled: bool) -> None:
        self._api.call("PATCH", f"/excludes/{exclude_id}", {"enabled": enabled})


class WatchTargetProxy:
    def __init__(self, api: SyncCaller) -> None:
        self._api = api

    def list_all(self) -> list[WatchTargetRecord]:
        return [_from_dict(WatchTargetRecord, item) for item in self._api.items("/realtime/targets")]

    def list_enabled_paths(self) -> list[str]:
        return [record.path for record in self.list_all() if record.enabled]

    def add(self, path: str) -> WatchTargetRecord:
        return _from_dict(WatchTargetRecord, self._api.call("POST", "/realtime/targets", {"path": path}))

    def remove(self, target_id: int) -> None:
        self._api.call("DELETE", f"/realtime/targets/{target_id}")

    def set_enabled(self, target_id: int, enabled: bool) -> None:
        self._api.call("PATCH", f"/realtime/targets/{target_id}", {"enabled": enabled})


class SchedulerProxy:
    def __init__(self, api: SyncCaller) -> None:
        self._api = api

    def list_all(self) -> list[ScheduleRecord]:
        return [_from_dict(ScheduleRecord, item) for item in self._api.items("/schedules")]

    def add_job(self, name: str, scan_type: str, cron: str, target: str = "", wake: bool = False) -> ScheduleRecord:
        body = {"name": name, "scan_type": scan_type, "cron": cron, "target": target, "wake": wake}
        return _from_dict(ScheduleRecord, self._api.call("POST", "/schedules", body))

    def update_job(self, job_id: int, name: str, scan_type: str, cron: str, target: str = "",
                   wake: bool | None = None) -> ScheduleRecord:
        if wake is None:
            # 編集画面に項目が無い間は、今の値のまま送る（PUT は全体を置き換えるため）
            wake = next((r.wake for r in self.list_all() if r.id == job_id), False)
        body = {"name": name, "scan_type": scan_type, "cron": cron, "target": target, "wake": wake}
        return _from_dict(ScheduleRecord, self._api.call("PUT", f"/schedules/{job_id}", body))

    def set_enabled(self, job_id: int, enabled: bool) -> None:
        self._api.call("PATCH", f"/schedules/{job_id}", {"enabled": enabled})

    def remove_job(self, job_id: int) -> None:
        self._api.call("DELETE", f"/schedules/{job_id}")

    def run_now(self, job_id: int) -> None:
        self._api.call("POST", f"/schedules/{job_id}/run")


class UpdateServiceProxy:
    def __init__(self, api: SyncCaller, status: StatusCache) -> None:
        self._api = api
        self._status = status

    def list_history(self) -> list[UpdateHistoryRecord]:
        return [_from_dict(UpdateHistoryRecord, item) for item in self._api.items("/updates/history")]

    def last_executed_at(self) -> datetime | None:
        value = self._status.get().get("update", {}).get("last_executed_at")
        return datetime.fromisoformat(value) if value else None


class FreshclamServiceProxy:
    """OS の定義更新サービス（core.freshclam_service.FreshclamServiceController の代わり）。状態と説明を dict で返す"""

    def __init__(self, api: SyncCaller) -> None:
        self._api = api

    def status(self) -> dict:
        return self._api.call("GET", "/updates/freshclam-service")

    def set_running(self, running: bool) -> dict:
        # admin の操作（昇格ヘルパーに頼む。結果は状態を読み直して確かめる）
        self._api.call("PUT", "/updates/freshclam-service", {"enabled": running})
        return self.status()


class ClamAVProxy:
    """ClamAVService の代わり（状態の参照だけ。検査はサービスが行う）"""

    def __init__(self, status: StatusCache) -> None:
        self._status = status

    def ping(self, retry: bool = True) -> bool:
        try:
            return bool(self._status.get()["clamd"]["reachable"])
        except AppError:
            return False

    def get_version(self) -> tuple[str, str, str]:
        clamd = self._status.get()["clamd"]
        return clamd.get("engine_version", ""), clamd.get("database_version", ""), clamd.get("database_date", "")

    @property
    def verified(self) -> bool | None:
        return self._status.get()["clamd"].get("verified")

    @property
    def endpoint(self) -> str:
        return "ClamQtPie サービス経由"

    def update_database(self) -> bool:
        raise AppError("定義の読み込み直しは、サービスが定義の更新の後に行います")


class LogReaderProxy(LogReaderService):
    """LogReaderService の代わり。読み取りは GET /logs、絞り込み・CSV の書き出しは手元で行う"""

    def __init__(self, api: SyncCaller) -> None:
        self._api = api

    def read_entries(self, since: date | None = None) -> list[LogEntry]:
        entries = []
        for item in self._api.items("/logs", params={"since": since.isoformat() if since else None}):
            try:
                when = datetime.fromisoformat(item["datetime"]).astimezone().replace(tzinfo=None)
            except (KeyError, ValueError):
                continue
            entries.append(LogEntry(datetime=when, level=item.get("level", ""), category=item.get("category", ""),
                                    error_code=item.get("error_code") or "", message=item.get("message", "")))
        return sorted(entries, key=lambda e: e.datetime)  # これまでどおり古い順

    def clear(self) -> int:
        return int(self._api.call("DELETE", "/logs").get("removed_files", 0))


class NotificationProxy:
    def __init__(self, api: SyncCaller) -> None:
        self._api = api

    def send_test_mail(self) -> None:
        self._api.call("POST", "/notifications/test-mail")


class ScanHistoryProxy:
    """スキャン履歴（GET /scans/history。スキャン画面の履歴の一覧。段階 12-4b）"""

    def __init__(self, api: SyncCaller) -> None:
        self._api = api

    def list_recent(self, limit: int = 200) -> list[SimpleNamespace]:
        """新しい順に limit 件"""
        page = self._api.call("GET", "/scans/history", params={"limit": limit})
        return [SimpleNamespace(**item) for item in (page.get("items", []) if isinstance(page, dict) else [])]

    def get(self, history_id: int) -> dict:
        """1 件の詳細（detections・error_files を含む）"""
        return self._api.call("GET", f"/scans/history/{history_id}")


# -- イベントから作るシグナル --------------------------------------------------------------------
class RealtimeProxy(QObject):
    """RealtimeManager の代わり。状態は GET /status・status イベント、検知などは realtime.* イベント"""

    detected = Signal(object, str)
    detection_handled = Signal(object, str, bool)
    watch_target_lost = Signal(object)
    scan_error = Signal(object)

    def __init__(self, api: SyncCaller, status: StatusCache, config: ConfigProxy, events: EventStream) -> None:
        super().__init__()
        self._api = api
        self._status = status
        self._config = config
        self._running: bool | None = None
        self._mode = ""
        events.event.connect(self._on_event)

    @property
    def is_running(self) -> bool:
        if self._running is None:
            try:
                realtime = self._status.get()["realtime"]
                self._running, self._mode = bool(realtime["running"]), realtime.get("mode", "")
            except AppError:
                return False
        return self._running

    @property
    def mode_name(self) -> str:
        self.is_running  # noqa: B018 - 状態を読んでおく
        return self._mode

    @property
    def config(self):
        return self._config.current.realtime

    def start(self) -> bool:
        return self._set(True)

    def stop(self) -> None:
        self._set(False)

    def _set(self, enabled: bool) -> bool:
        result = self._api.call("PUT", "/realtime", {"enabled": enabled})
        self._running = bool(result.get("running", enabled)) if isinstance(result, dict) else enabled
        self._status.invalidate()
        return self._running

    def add_target(self, path: str) -> None:
        pass  # POST /realtime/targets でサービスが監視にも加える

    def remove_target(self, path: str) -> None:
        pass

    def _on_event(self, name: str, data) -> None:
        if name == "status":
            realtime = data.get("realtime", {})
            self._running, self._mode = bool(realtime.get("running")), realtime.get("mode", self._mode)
        elif name == "realtime.detected":
            self.detected.emit(data.get("path", ""), data.get("virus_name", ""))
        elif name == "realtime.handled":
            self.detection_handled.emit(data.get("path", ""), data.get("virus_name", ""), bool(data.get("quarantined")))
        elif name == "realtime.target_lost":
            self.watch_target_lost.emit(data.get("path", ""))
        elif name == "realtime.error":
            self.scan_error.emit(AppError(data.get("message") or None, code=data.get("code", "E2000"),
                                          detail=data.get("reason")))


class ScanDispatcherProxy(QObject):
    """ScanDispatcher の代わり。開始は POST /scans、進み具合などは scan.* イベント"""

    progress = Signal(int, int, object)
    file_scanned = Signal(object, str, str, str)
    finished = Signal(dict)
    error = Signal(object)
    activity = Signal(str, object, int)
    started = Signal(str)

    def __init__(self, api: SyncCaller, events: EventStream) -> None:
        super().__init__()
        self._api = api
        self._running = False
        events.event.connect(self._on_event)

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self, targets, include_subfolders: bool = True, apply_exclude: bool = True, label: str = "",
              skip_dirs=(), scan_type: str = "custom", **_kwargs) -> None:
        targets = [str(t) for t in targets]
        if scan_type == "quick":
            # 画面が求めた場所（デスクトップ・ダウンロード等）のうち、自分のフォルダの中だけ（Linux の /tmp など、
            # 外の場所はサービスが user のトークンでは断るため。§64）
            from core.peer_identity import is_inside

            home = str(Path.home())
            targets = [t for t in targets if is_inside(t, home)] or targets
        body = {"type": scan_type, "targets": targets,
                "include_subfolders": include_subfolders, "apply_exclude": apply_exclude, "source": "gui"}
        if scan_type == "full":
            body.pop("targets")  # フルスキャンの対象はサービスが決める
        self._running = True
        try:
            self._api.call("POST", "/scans", body)
        except AppError as exc:
            self._running = False
            self.error.emit(exc)
            self.finished.emit({"scanned": 0, "detected": 0, "errors": 0, "cancelled": True})

    def stop(self) -> None:
        try:
            self._api.call("POST", "/scans/current/cancel")
        except AppError as exc:
            self.error.emit(exc)

    def _on_event(self, name: str, data) -> None:
        if name == "scan.started":
            self._running = True
            self.started.emit(data.get("job", {}).get("label", ""))
        elif name == "scan.progress":
            self.progress.emit(int(data.get("scanned", 0)), int(data.get("total", 0)), data.get("current_path", ""))
        elif name == "scan.file":
            self.file_scanned.emit(data.get("path", ""), data.get("status", ""), data.get("detail", ""),
                                   data.get("detected_at", ""))
        elif name == "scan.activity":
            self.activity.emit(data.get("state", ""), data.get("path", ""), int(data.get("seconds", 0)))
        elif name == "scan.finished":
            self._running = False
            self.finished.emit({key: data.get(key) for key in ("scanned", "detected", "errors", "cancelled",
                                                               "history_id", "state")})


class UpdateDispatcherProxy(QObject):
    """UpdateDispatcher の代わり。開始は POST /updates、ログ・結果は update.* イベント"""

    log_line = Signal(str)
    finished = Signal(object)
    started = Signal()

    def __init__(self, api: SyncCaller, events: EventStream) -> None:
        super().__init__()
        self._api = api
        self._running = False
        events.event.connect(self._on_event)

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        self._running = True
        try:
            self._api.call("POST", "/updates")
        except AppError as exc:
            self._running = False
            self.finished.emit(UpdateHistoryRecord(id=0, executed_at="", result="failure", before_version="",
                                                   after_version="", detail=f"{exc.code}: {exc.message}"))

    def _on_event(self, name: str, data) -> None:
        if name == "update.started":
            self._running = True
            self.started.emit()
        elif name == "update.log":
            self.log_line.emit(data.get("line", ""))
        elif name == "update.finished":
            self._running = False
            self.finished.emit(_from_dict(UpdateHistoryRecord, data.get("record", {})))


class ServiceProxies:
    """トレイGUI が使うプロキシ一式（画面のスレッドで作る）"""

    def __init__(self, api: SyncCaller, events: EventStream, gui_path: Path | None = None) -> None:
        self.api = api
        self.events = events
        self.status = StatusCache(api)
        self.config = ConfigProxy(api, gui_path)
        self.quarantine = QuarantineProxy(api)
        self.excludes = ExcludeProxy(api)
        self.watch_targets = WatchTargetProxy(api)
        self.scheduler = SchedulerProxy(api)
        self.update_service = UpdateServiceProxy(api, self.status)
        self.freshclam_service = FreshclamServiceProxy(api)
        self.clamav = ClamAVProxy(self.status)
        self.log_reader = LogReaderProxy(api)
        self.notification = NotificationProxy(api)
        self.scan_history = ScanHistoryProxy(api)
        self.realtime = RealtimeProxy(api, self.status, self.config, events)
        self.scan = ScanDispatcherProxy(api, events)
        self.update = UpdateDispatcherProxy(api, events)
        events.event.connect(self._on_event)

    def _on_event(self, name: str, _data) -> None:
        if name == "config.changed":
            self.config.invalidate()
        elif name in ("status", "update.finished", "scan.finished"):
            self.status.invalidate()

