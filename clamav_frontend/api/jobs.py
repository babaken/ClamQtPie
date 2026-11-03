"""
時間のかかる処理のジョブ（サービス化・Web API設計書 4.3節・5章。段階 12-2b）

スキャン（ScanDispatcher）と定義の更新（UpdateDispatcher）を、開始・進み具合・終了のジョブとして記録し、
イベント（EventHub）を出す。画面・トレイ・Web API のどこから始めたものも記録する（開始の合図 started を受けて作る）。

- ジョブの state: running → finished | cancelled | failed。終わったジョブは種類ごとに直近 1 件を残す
- シグナルはメインスレッドで受ける（dispatcher はメインスレッドにあり、ワーカーのシグナルはキューで届く）。
  Web API の読み取り（スレッドプール）と重なるため、ジョブの内容はロックの中で読み書きする
- イベントに入れるファイル名・メッセージは、制御文字を見える形にする（1件のイベントが複数行に見えないように。12章 41）
"""
from __future__ import annotations

import itertools
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone

from api.bridge import EventHub
from core.scan_history_service import is_fatal_scan_error
from core.text_safety import visible_controls

KIND_SCAN = "scan"
KIND_UPDATE = "update"

STATE_RUNNING = "running"
STATE_FINISHED = "finished"
STATE_CANCELLED = "cancelled"
STATE_FAILED = "failed"

#: どこから始めたか（Web API から始めたもの以外は画面・トレイ）
SOURCE_GUI = "gui"
SOURCE_API = "api"

#: scan.progress を出す最短の間隔（秒）。1秒に最大2回に間引く（5章）
PROGRESS_INTERVAL_SEC = 0.5

#: scan.file で知らせる結果（検知とエラーだけ。OK のファイルは数が多いため送らない）
_NOTABLE_STATUSES = ("FOUND", "ERROR")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobTracker:
    def __init__(self, events: EventHub, clock=time.monotonic, last_scan_history_id=None) -> None:
        """last_scan_history_id: 直前に記録したスキャン履歴の ID を返す関数（scan.finished に付ける。12-2c）。
        履歴の記録（ScanHistoryRecorder）は先に finished を受けるよう、こちらより先に接続しておく"""
        self._events = events
        self._last_scan_history_id = last_scan_history_id
        self._clock = clock
        self._lock = threading.Lock()
        self._seq = itertools.count(1)
        self._jobs: dict[str, dict | None] = {KIND_SCAN: None, KIND_UPDATE: None}
        self._pending_source: dict[str, str] = {}
        self._last_progress_at = float("-inf")

    # -- 接続（サービスの本体から、メインスレッドで） --------------------------------------
    def connect(self, scan_dispatcher, update_dispatcher) -> None:
        scan_dispatcher.started.connect(self.on_scan_started)
        scan_dispatcher.progress.connect(self.on_scan_progress)
        scan_dispatcher.file_scanned.connect(self.on_scan_file)
        scan_dispatcher.activity.connect(self.on_scan_activity)
        scan_dispatcher.error.connect(self.on_scan_error)
        scan_dispatcher.finished.connect(self.on_scan_finished)
        update_dispatcher.started.connect(self.on_update_started)
        update_dispatcher.log_line.connect(self.on_update_log)
        update_dispatcher.finished.connect(self.on_update_finished)

    # -- 参照（任意のスレッドから） ----------------------------------------------------------
    def current(self, kind: str) -> dict | None:
        """実行中（なければ直近）のジョブの写し"""
        with self._lock:
            job = self._jobs[kind]
            return None if job is None else {**job, "progress": dict(job["progress"])}

    def running_job_id(self, kind: str) -> str | None:
        with self._lock:
            job = self._jobs[kind]
            return job["id"] if job is not None and job["state"] == STATE_RUNNING else None

    def expect_source(self, kind: str, source: str) -> None:
        """次に始まるジョブの出どころ（Web API が dispatcher を呼ぶ直前に、メインスレッドで呼ぶ）"""
        with self._lock:
            self._pending_source[kind] = source

    # -- スキャン ---------------------------------------------------------------------------
    def _begin(self, kind: str, label: str, progress: dict) -> dict:
        with self._lock:
            started = datetime.now(timezone.utc)
            job = {
                "id": f"{kind}-{started.strftime('%Y%m%d-%H%M%S')}-{next(self._seq)}",
                "kind": kind,
                "label": label,
                "source": self._pending_source.pop(kind, SOURCE_GUI),
                "state": STATE_RUNNING,
                "started_at": started.isoformat(),
                "finished_at": None,
                "progress": progress,
                "error": None,
                "history_id": None,
            }
            self._jobs[kind] = job
            return {**job, "progress": dict(progress)}

    def _running(self, kind: str) -> dict | None:
        job = self._jobs[kind]
        return job if job is not None and job["state"] == STATE_RUNNING else None

    def on_scan_started(self, label: str) -> None:
        job = self._begin(KIND_SCAN, label, {"scanned": 0, "total": 0, "detected": 0, "errors": 0})
        self._last_progress_at = float("-inf")
        self._events.publish("scan.started", {"job": job})

    def on_scan_progress(self, scanned: int, total: int, current_path: str) -> None:
        with self._lock:
            job = self._running(KIND_SCAN)
            if job is None:
                return
            job["progress"].update(scanned=scanned, total=total)
            now = self._clock()
            if now - self._last_progress_at < PROGRESS_INTERVAL_SEC:
                return
            self._last_progress_at = now
            job_id = job["id"]
        self._events.publish("scan.progress", {"job_id": job_id, "scanned": scanned, "total": total,
                                               "current_path": visible_controls(current_path)})

    def on_scan_file(self, path: str, status: str, detail: str, detected_at: str) -> None:
        with self._lock:
            job = self._running(KIND_SCAN)
            if job is None or status not in _NOTABLE_STATUSES:
                return
            job["progress"]["detected" if status == "FOUND" else "errors"] += 1
            job_id = job["id"]
        self._events.publish("scan.file", {"job_id": job_id, "path": visible_controls(path), "status": status,
                                           "detail": visible_controls(detail), "detected_at": detected_at})

    def on_scan_activity(self, state: str, path: str, seconds: int) -> None:
        job_id = self.running_job_id(KIND_SCAN)
        if job_id is not None:
            self._events.publish("scan.activity", {"job_id": job_id, "state": state,
                                                   "path": visible_controls(path), "seconds": seconds})

    def on_scan_error(self, error) -> None:
        with self._lock:
            job = self._running(KIND_SCAN)
            # 1ファイルのエラー（応答待ちのタイムアウトなど）ではジョブを失敗にしない
            if job is not None and is_fatal_scan_error(error, job["progress"]["total"] > 0):
                job["error"] = {"code": getattr(error, "code", "E9001"), "message": getattr(error, "message", "")}

    def on_scan_finished(self, summary: dict) -> None:
        with self._lock:
            job = self._running(KIND_SCAN)
            if job is None:
                return
            job["progress"].update(scanned=summary.get("scanned", 0), detected=summary.get("detected", 0),
                                   errors=summary.get("errors", 0))
            if job["error"] is not None:
                job["state"] = STATE_FAILED
            elif summary.get("cancelled"):
                job["state"] = STATE_CANCELLED
            else:
                job["state"] = STATE_FINISHED
            job["finished_at"] = _now()
            job["history_id"] = self._last_scan_history_id() if self._last_scan_history_id else None
            data = {"job_id": job["id"], "state": job["state"], "scanned": job["progress"]["scanned"],
                    "detected": job["progress"]["detected"], "errors": job["progress"]["errors"],
                    "cancelled": bool(summary.get("cancelled")), "history_id": job["history_id"]}
        self._events.publish("scan.finished", data)

    # -- 定義の更新 -------------------------------------------------------------------------
    def on_update_started(self) -> None:
        job = self._begin(KIND_UPDATE, "定義更新", {"lines": 0})
        self._events.publish("update.started", {"job": job})

    def on_update_log(self, line: str) -> None:
        with self._lock:
            job = self._running(KIND_UPDATE)
            if job is None:
                return
            job["progress"]["lines"] += 1
            job_id = job["id"]
        self._events.publish("update.log", {"job_id": job_id, "line": visible_controls(line)})

    def on_update_finished(self, record) -> None:
        with self._lock:
            job = self._running(KIND_UPDATE)
            if job is None:
                return
            job["state"] = STATE_FAILED if record.result == "failure" else STATE_FINISHED
            job["finished_at"] = _now()
            if record.result == "failure":
                job["error"] = {"code": "E8004", "message": "定義データベースの更新に失敗しました"}
            data = {"job_id": job["id"], "state": job["state"], "record": _update_record(record)}
        self._events.publish("update.finished", data)


def _update_record(record) -> dict:
    data = asdict(record)
    # detail は freshclam の出力（ERROR・WARNING の行）。制御文字を見える形にして返す
    data["detail"] = visible_controls(data.get("detail") or "")
    return data
