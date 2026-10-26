# 基本設計書

# 1. 画面一覧

  画面           概要
  -------------- --------------------------------------
  ホーム         状態・定義更新日時・リアルタイム状態
  スキャン       ファイル/フォルダスキャン、進捗表示
  リアルタイム   ON/OFF、監視・除外設定
  スケジュール   定期スキャン・定義更新ジョブ管理
  隔離           隔離一覧、復元、削除
  定義更新       手動更新、更新履歴
  ログ           検索・フィルタ・CSV出力
  設定           全体設定、SMTP、自動起動等

# 2. 画面遷移

``` text
ホーム
├─ スキャン
├─ リアルタイム
├─ スケジュール
├─ 隔離
├─ 定義更新
├─ ログ
└─ 設定

タスクトレイ
├─ メイン画面
├─ 今すぐスキャン
├─ 定義更新
├─ リアルタイム ON/OFF
└─ 終了
```

# 3. モジュール構成

``` text
app.py
gui/
  main_window.py
  tray.py
  pages/
core/
  clamav_service.py
  realtime.py
  scheduler.py
  updater.py
  quarantine.py
  logger.py
  notification.py
db/
  database.py
models/
utils/
config/
logs/
quarantine/
```

## 主な責務

-   GUI: 画面表示・イベント
-   ClamAVService: clamd制御、スキャン、更新、状態取得
-   Realtime: Linux=clamonacc、Windows=watchdog制御
-   Scheduler: APScheduler管理
-   Quarantine: 隔離・復元
-   Logger: ログ出力
-   Notification: メール・通知

# 4. DB設計(SQLite)

## quarantine

  列              型
  --------------- ------------
  id              INTEGER PK
  uuid            TEXT
  original_path   TEXT
  virus_name      TEXT
  sha256          TEXT
  size            INTEGER
  detected_at     TEXT
  restored        INTEGER
  deleted         INTEGER

## schedule

\|列\|型\| \|id\|INTEGER PK\| \|name\|TEXT\| \|enabled\|INTEGER\|
\|cron\|TEXT\| \|target\|TEXT\| \|scan_type\|TEXT\| \|last_run\|TEXT\|
\|next_run\|TEXT\|

## exclude

\|列\|型\| \|id\|INTEGER PK\| \|type\|TEXT\| \|value\|TEXT\|
\|enabled\|INTEGER\|

# 5. アーキテクチャ

``` text
PySide6 GUI
    │
Controller
    │
ClamAVService
 ├─ clamd
 ├─ freshclam
 ├─ clamonacc(Linux)
 └─ watchdog(Windows)

SQLite
APScheduler
logging
```

# 6. Phase 11 改訂（実際のモジュール構成）

3章のモジュール構成は実装時に以下となった（`gui/pages/`は`gui/screens/`、`core/realtime.py`等は役割ごとに分割）。

``` text
app.py                     起動シーケンス・サービスの生成と画面への結線
core/
  clamd_client.py          clamdプロトコル層（PING/VERSION/SCAN/MULTISCAN/INSTREAM/RELOAD）
  clamav_service.py        clamd通信のサービス層（リトライ・同時接続数・INSTREAMフォールバック）
  clamd_process.py         clamdサービスの起動
  scan_dispatcher.py       オンデマンドスキャン（QThread）
  scan_targets.py          クイック/フルスキャンの対象
  scheduled_scan.py        スケジュールスキャンの検知時処理
  realtime_manager.py      リアルタイム監視（watchdog、Windows／Linuxのフォールバック）
  clamonacc_manager.py     リアルタイム監視（Linux、clamonacc）
  realtime_factory.py      リアルタイム監視方式の選択
  scheduler_service.py     APScheduler管理
  update_service.py        freshclamによる定義更新・更新履歴
  quarantine_service.py    隔離・復元・完全削除
  exclude_service.py       除外リスト
  watch_target_service.py  リアルタイム監視の対象パス
  notification_service.py  デスクトップ通知・メール通知（detection_aggregator.pyで集約）
  config_model.py / config_service.py  config.json
  logger.py / log_reader_service.py    ログ出力・ログ画面用の読込
  autostart_service.py     OS起動時の自動起動
  single_instance.py       多重起動防止
  exceptions.py            例外・エラーコード
db/
  database.py              接続・Alembicマイグレーション
  models/                  quarantine / schedule / exclude / watch_target / update_history
gui/
  main_window.py / tray.py / error_dialog.py
  screens/                 ホーム・スキャン・リアルタイム・スケジュール・隔離・定義更新・ログ・設定
  dialogs/                 ジョブ編集ダイアログ
alembic/                   マイグレーション（0001〜0003）
icons/                     アイコン（scripts/generate_icons.pyで生成）
```

- 4章のDB設計に加え、`watch_target`（Windowsリアルタイム監視詳細設計書3.2節）・`update_history`（DBマイグレーション設計書9.1節）テーブルがある
- 5章のアーキテクチャ「clamonacc(Linux)」は「Linuxリアルタイム監視詳細設計書」を参照
