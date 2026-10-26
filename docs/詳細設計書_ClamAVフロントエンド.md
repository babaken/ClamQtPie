# 詳細設計書

# 1. クラス設計

## ClamAVService

-   start_clamd()
-   stop_clamd()
-   restart_clamd()
-   ping()
-   scan_file(path)
-   scan_directory(path, recursive=True)
-   update_database()
-   start_realtime()
-   stop_realtime()
-   get_version()
-   get_database_version()

## RealtimeManager

-   start()
-   stop()
-   set_enabled(bool)
-   handle_event(path)

## SchedulerService

-   add_job()
-   update_job()
-   delete_job()
-   run_job_now()
-   load_jobs()

## QuarantineService

-   quarantine(path, virus)
-   restore(id)
-   delete(id)
-   list()

## DatabaseService

-   connect()
-   execute()
-   query()
-   migrate()

## NotificationService

-   send_mail()
-   notify_desktop()

# 2. シーケンス図

## ファイルスキャン

``` text
GUI -> ClamAVService : scan_file()
ClamAVService -> clamd : SCAN
clamd --> ClamAVService : Result
ClamAVService -> Logger : write
ClamAVService -> GUI : Result
```

## リアルタイム監視

``` text
Linux: clamonacc -> clamd -> ClamAVService -> GUI通知
Windows: watchdog -> ClamAVService -> clamd -> Logger -> GUI通知
```

## 定義更新

``` text
GUI -> ClamAVService
ClamAVService -> freshclam
freshclam --> ClamAVService
ClamAVService -> Logger
GUIへ完了通知
```

## 隔離

``` text
clamd検知
 -> QuarantineService
 -> UUIDへ変更
 -> Quarantine保存
 -> SQLite登録
 -> GUI通知
```

# 3. API設計（内部）

  メソッド                概要
  ----------------------- --------------
  scan_file(path)         単一ファイル
  scan_directory(path)    フォルダ
  update_database()       定義更新
  start_realtime()        監視開始
  stop_realtime()         監視停止
  quarantine(path)        隔離
  restore(id)             復元
  delete_quarantine(id)   削除
  add_schedule(job)       ジョブ追加
  remove_schedule(id)     ジョブ削除
  get_logs(filter)        ログ取得
  get_status()            サービス状態

# 4. エラー方針

-   clamd未起動: GUIへ通知・再起動試行
-   freshclam失敗: ログ・通知
-   DB障害: ロールバック
-   権限不足: エラーダイアログ

# 5. ログレベル

-   DEBUG
-   INFO
-   WARNING
-   ERROR
-   CRITICAL

------------------------------------------------------------------------

# 6. Phase 11 改訂（実装との対応）

## 6.1 クラスと実装モジュールの対応

| 本書のクラス・メソッド | 実装 |
|---|---|
| ClamAVService.start_clamd/restart_clamd | `core/clamd_process.py` ClamdServiceController.start()（OSのサービスとして起動）、ClamdStarter（バックグラウンド実行）。stop_clamdは実装しない（clamdはOSのサービスとして管理し、本アプリからは停止しない） |
| ClamAVService.start_realtime/stop_realtime | RealtimeManager／ClamonaccRealtimeManagerのstart/stop（`core/realtime_factory.py`でOSに応じて選択） |
| RealtimeManager.set_enabled/handle_event | start/stopと監視対象の差分反映（add_target/remove_target）に置き換え |
| SchedulerService.load_jobs | start()時に有効なジョブを読み込む。自動更新ジョブはset_auto_update() |
| QuarantineService.delete/list | delete_permanently()/list_all()。restore()は復元先の同名ファイルを上書きしない（overwrite=Trueで上書き） |
| NotificationService.send_mail/notify_desktop | notify_detection()（集約あり）／notify_update_failure()／notify_job_failure()／send_test_mail() |
| get_logs(filter) | `core/log_reader_service.py` LogReaderService.read_entries()/filter_entries() |
| get_status() | ホーム画面のrefresh_status()がAppStatus（clamd稼働・定義バージョン・監視ON/OFF）を発行し、ステータスバー・トレイへ反映 |

## 6.2 シーケンスの追加・変更

### スケジュールスキャン（追加）

```text
APScheduler -> SchedulerService._execute()
 -> run_scheduled_scan()（core/scheduled_scan.py）
     -> ScanWorker.run()（ファイル単位でSCAN、ログ記録）
     -> 検知ごと: QuarantineService.quarantine()（検知時アクションが自動隔離の場合）
                 NotificationService.notify_detection()
 -> 最終実行結果を記録（成功／失敗／検知あり n件）
 -> GUIへ通知（トレイ「検知あり」、ホーム画面のアクティビティ）
```

### ファイルスキャン（変更）

```text
GUI -> ScanDispatcher(QThread) -> ScanWorker
ScanWorker -> ClamAVService : scan_file()（1ファイルずつ）
ClamAVService -> clamd : SCAN（Permission deniedならINSTREAMで再送）
ScanWorker -> Logger : 開始・検知・完了をカテゴリ「スキャン」で記録
ScanWorker -> GUI : 進捗・検知（GUI側は200ms間隔でまとめて表示）
```

## 6.3 エラー方針（4章）の実装
- clamd未起動: ホーム画面・ステータスバー・トレイに表示し、サービスとして起動を1回試みる（E1006/E8003）
- freshclam失敗: ログ・通知（E8004、Phase 11でE8003から分離）
- DB障害: トランザクションのロールバック（隔離時のDB登録失敗はファイル移動も戻す、E3004）
- 権限不足: エラーダイアログ。SCANの権限エラーはINSTREAMで再試行する
