"""
トレイGUI の画面のテストで使う、サービスの API の代わり（段階 12-4e）

画面は変更をためておき、［変更を保存］で操作の一覧（api_client.admin_ops.Operation）をまとめて送る。
FakeServiceApi は、その操作を手元の本物のサービス（ConfigService・WatchTargetService・ExcludeService・
SchedulerService）に適用する。画面のテストで、保存した結果をサービスの内容で確かめられる。
"""
from __future__ import annotations

from core.config_model import ConfigModel


class FakeServiceApi:
    def __init__(self, config_service=None, watch_target_service=None, exclude_service=None,
                 scheduler_service=None) -> None:
        self.config_service = config_service
        self.watch_target_service = watch_target_service
        self.exclude_service = exclude_service
        self.scheduler_service = scheduler_service
        #: 送られた操作の一覧（batch 1 回ごと）
        self.batches: list[list] = []
        self.smtp_password: str | None = None
        self.protected: list[str] = []

    # -- SyncCaller と同じ形 ------------------------------------------------------------------
    def call(self, method: str, path: str, body=None):
        return self._apply(method, path, body)

    def batch(self, operations) -> list:
        self.batches.append(list(operations))
        return [self._apply(op.method, op.path, op.body) for op in operations]

    # -- 操作の適用 -------------------------------------------------------------------------------
    def _apply(self, method: str, path: str, body):
        parts = path.strip("/").split("/")
        key = int(parts[-1]) if parts[-1].isdigit() else None
        if path == "/config":
            if method == "GET":
                data = self.config_service.as_dict()
                data["protected"] = list(self.protected)
                return data
            model = ConfigModel.from_dict(body)
            model.general = self.config_service.current.general
            self.config_service.save(model)
            return {}
        if path == "/config/smtp-password":
            self.smtp_password = body["password"]
            return {}
        if parts[:2] == ["realtime", "targets"]:
            return self._apply_crud(self.watch_target_service, method, key, body,
                                    add=lambda b: self.watch_target_service.add(b["path"]))
        if parts[0] == "excludes":
            return self._apply_crud(self.exclude_service, method, key, body,
                                    add=lambda b: self.exclude_service.add(b["type"], b["value"], b.get("enabled", True)))
        if parts[0] == "schedules":
            scheduler = self.scheduler_service
            if method == "PUT":
                return scheduler.update_job(key, body["name"], body["scan_type"], body["cron"],
                                            target=body.get("target", ""), wake=body.get("wake", False))
            if method == "DELETE":
                return scheduler.remove_job(key)
            return self._apply_crud(scheduler, method, key, body,
                                    add=lambda b: scheduler.add_job(b["name"], b["scan_type"], b["cron"],
                                                                    target=b.get("target", ""), wake=b.get("wake", False)))
        raise AssertionError(f"テストの API に無い操作です: {method} {path}")

    @staticmethod
    def _apply_crud(service, method, key, body, add):
        if method == "POST":
            return add(body)
        if method == "DELETE":
            return service.remove(key)
        if method == "PATCH":
            return service.set_enabled(key, body["enabled"])
        raise AssertionError(f"テストの API に無い操作です: {method}")


def config_proxy(tmp_path, config_service):
    """トレイGUI と同じ ConfigProxy を、FakeServiceApi 越しに config_service につなぐ（GUI の設定は gui.json）"""
    from api_client.proxies import ConfigProxy

    api = FakeServiceApi(config_service=config_service)
    return ConfigProxy(api, gui_path=tmp_path / "gui.json"), api
