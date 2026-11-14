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

------------------------------------------------------------------------

# 7. 段階 12 改訂（v0.10.0: サービス化）

v0.10.0 で、1〜6 章のクラス（ClamAVService・RealtimeManager・SchedulerService など）はサービスのプロセスで動くようになり、画面（トレイGUI・WebUI）は Web API を通して使う形になった。1 章・6.1 節のクラスの役割は引き続き有効。3 章の「API設計（内部）」と 2 章・6.2 節のシーケンスは、この章が優先する。プロセスの構成は基本設計書 7 章、実装の記録は `clamav_frontend/README_DEV.md` の §55〜§91。

## 7.1 クラスと実装モジュールの対応（6.1 節の追加）

| 役割 | 実装 |
|---|---|
| サービスの部品の組み立て・結線（6.1 節で GUI の `app.py` が行っていたこと） | `core/runtime.py` の `build(paths)`。検知 → 通知・保存、スケジュールの失敗、スキャンの通知などを結ぶ |
| 内部 API・WebUI の待ち受け | `api/service_api.py`（`ServiceApi`）、`api/server.py`（`ApiServer`。uvicorn をスレッドで動かす）、`api/web_control.py`（WebUI の ON/OFF・タイマー・HTTPS・LAN） |
| Web API の処理 | `api/routes/`（読み取り・書き込み・ジョブ・通知・WebUI のログイン）、`api/actions.py`（変更の処理）、`api/jobs.py`（スキャン・定義更新を 1 つずつ実行）、`api/events.py`（イベント） |
| 認証 | `api/auth.py`（内部 API は Bearer のトークン、WebUI は Cookie のセッションと CSRF トークン）、`api/web_sessions.py` |
| 画面からの呼び出し | `api_client/proxies.py`（1 章のサービスと同じ名前・形のプロキシ）、`api_client/sync.py`（画面のスレッドから同期的に呼ぶ）、`api_client/client.py`（`ApiClient`）、`api_client/events.py`（`EventStream`） |
| 管理者の操作 | `api_client/elevation.py`（昇格ヘルパーの起動と受け渡し）、`elevate_main.py`（確認画面と実行）、`api_client/admin_ops.py`（`ADMIN_ROUTES`・`describe()`） |
| 通知の保存 | `core/notification_store.py`。`NotificationService(desktop_sink=...)` は、サービスではデスクトップに出す代わりに保存する |
| get_status() | `GET /status`（clamd の稼働・確かめてから接続しているか・定義の版と古さ・リアルタイム保護・WebUI の状態）と、2 秒ごとの `status` イベント |

## 7.2 シーケンスの変更（2 章・6.2 節の置き換え）

### 画面からのスキャン

```text
トレイGUI（スキャン画面）-> ScanProxy -> ApiClient : POST /scans（user のトークン）
  ※ 自分のフォルダの外・フルスキャンは 403（E8102）→ 昇格ヘルパーで admin のトークンを使ってやり直す
サービス : 対象を確かめる -> api/jobs.py -> ScanWorker（1 ファイルずつ clamd へ。6.2 節と同じ）
サービス -> EventStream : scan.started / scan.progress / scan.file / scan.finished
サービス -> DB : スキャン履歴
トレイGUI : イベントで進み具合・検知を表示（200ms 間隔でまとめる）
```

### 管理者の操作（トレイGUI）

```text
画面（変更をためておく）-> ［変更を保存］
 -> ElevatedCaller : 操作の一覧（メソッド・パス・本文）
 -> 昇格ヘルパーを UAC（Linux は pkexec）で起動し、名前付きパイプ（Linux は標準入出力）で一覧を渡す
 -> 昇格ヘルパー : すべてが ADMIN_ROUTES にあるかを確かめる
                  admin のトークンを自分で読み、接続先がサービスかを確かめる
                  確認画面（自分で作った説明）→「実行」
 -> サービス : 順に実行（途中で失敗したら残りは実行しない）
 -> 昇格ヘルパー -> トレイGUI : 結果だけを返す（トークンは返さない）
```

### 管理者の操作（WebUI）

```text
ブラウザ -> サービス : 操作（Cookie のセッション、CSRF トークン）
サービス -> ブラウザ : 403（E8108。確認から 5 分を過ぎている）
ブラウザ : 操作の内容とパスワード（二段階認証を設定していれば確認コード）を求める
ブラウザ -> サービス : POST /auth/reauth -> 同じ操作をもう一度
```

### 検知（リアルタイム保護・スケジュールのスキャン）

```text
watchdog / clamonacc -> サービス -> clamd で確かめる
 -> 検知時アクション（自動隔離／通知のみ）
 -> NotificationService : メール（設定していれば）、通知を保存（pending_notification）
 -> イベント notification
 -> トレイGUI : デスクトップに通知（未読をまとめる）、WebUI : 未読の件数
 -> 既読（トレイGUI・WebUI）: POST /notifications/ack -> イベント notification.acked -> 通知センターから消す
```

### 定義更新

```text
画面 -> POST /updates -> サービス : freshclam を実行（api/jobs.py。1 つずつ）
サービス -> イベント : update.started / update.log（freshclam の出力）/ update.finished
※ Linux で OS の clamav-freshclam が動いている間は、自動・スケジュールの更新は行わない（ログに 1 回残す）
```

## 7.3 Web API（3 章の置き換え）

接続先は `/api/v1`。内部 API（127.0.0.1、トークン）と WebUI（Cookie のセッション）で同じ処理を使う。ロールは user（トレイGUI を使える利用者）と admin（昇格ヘルパー・`clamqtpie-ctl`・WebUI のログイン）。admin のロールが要る操作は `api_client/admin_ops.py` の `ADMIN_ROUTES`。

| 区分 | API | ロール |
|---|---|---|
| 状態 | `GET /version`・`GET /status`・`GET /events`（SSE） | user |
| 読み取り | `GET /config`・`/excludes`・`/realtime`・`/realtime/targets`・`/schedules`・`/quarantine`・`/logs`・`/logs/export.csv`・`/scans/history`・`/updates/history`・`/updates/freshclam-service` | user |
| スキャン | `POST /scans`（user は頼んだ利用者のフォルダの中だけ。フルスキャンは admin）・`GET /scans/current`・`POST /scans/current/cancel` | user（一部 admin） |
| 隔離（スキャン結果から） | `POST /quarantine`（user は頼んだ利用者のフォルダの中だけ） | user（一部 admin） |
| 定義更新 | `POST /updates`・`GET /updates/current` | user |
| 通知 | `GET /notifications/pending`・`POST /notifications/ack` | user |
| 変更 | 設定（`PUT /config`・SMTP パスワード）、除外・監視するフォルダ・スケジュールの追加・変更・削除、リアルタイム保護の開始と停止、隔離の復元と削除、clamd の開始と停止、OS の定義更新サービスの開始と停止、ヘルスチェック、テストのメール | admin |
| WebUI | `POST/GET/DELETE /auth/session`・`POST /auth/reauth`（WebUI の待ち受けだけ）、`GET/PUT /web`・`PUT /web/credentials`・`/web/totp`・`/web/tls`・`/web/unlock`（設定は内部 API の admin のトークンだけ） | ― |

- Web API で変えられる設定は `core/config_model.py` の `WRITABLE_FIELDS` だけ。それ以外（接続の設定・実行ファイルの場所・`api.*`）は `clamqtpie-ctl config set` で変える
- エラーの応答は `{"error": {"code", "message", "detail"}}`。コードはエラーコード一覧・例外設計書 8 章

## 7.4 エラー方針（4 章・6.3 節の追加）

- 権限不足: サービスが断る（E8102）。トレイGUI は、昇格できる操作なら昇格してやり直す。取り消しは E8111（ダイアログを出さない）、昇格ヘルパーの失敗は E8112
- サービスにつながらない: E8105。トレイGUI は 5 秒ごとにつなぎ直す。トレイGUI を使う権限が無い: E8110
- clamd 未起動: サービスが 1 回起動を試みる（6.3 節と同じ）。clamd の接続先が clamd であることを確かめられなければ接続しない（E1001、理由つき）
- サービスが断った要求は、その機能のカテゴリでサービスのログに残す（ログ画面で見つかるように）
