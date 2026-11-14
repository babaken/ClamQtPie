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

# 7. 段階 12 改訂（v0.10.0: サービス化）

v0.10.0 で、1 つのアプリ（管理者権限で動く GUI が自分で監視・スキャン・スケジュールを動かす形）を、サービス・トレイGUI・WebUI などに分けた。1〜6 章のうち、画面一覧（1 章）・画面遷移（2 章）・モジュール構成（3 章・6 章）・DB 設計（4 章）・アーキテクチャ（5 章）は、この章が優先する。実装の記録は `clamav_frontend/README_DEV.md` の §55〜§91。

## 7.1 プロセスの構成（5 章の置き換え）

``` text
[サービス]  Windows サービス（LocalSystem）／systemd の unit（root）
  ├─ 監視・スキャン・スケジュール・定義更新・隔離・通知（core/、db/）
  ├─ 内部 API（127.0.0.1。トレイGUI・昇格ヘルパー・clamqtpie-ctl が使う。トークンで認証）
  └─ WebUI の待ち受け（既定 OFF。ブラウザが使う。ログインのセッションで認証）
        │ HTTP＋イベント（SSE）
        ├─ [トレイGUI]       一般の利用者の権限。画面とタスクトレイ。内部 API だけを使う
        ├─ [昇格ヘルパー]    管理者の操作のときだけ、UAC（Linux は polkit）の後に起動し、確認画面を出す
        ├─ [clamqtpie-ctl]   管理用コマンド（管理者・root で実行）
        └─ [ブラウザ]        WebUI（このPC、試験的に LAN の端末）
```

| プロセス | 権限 | 役割 |
|---|---|---|
| サービス（`service_main.py`） | LocalSystem / root | 保護の処理をすべて行う。PC の起動時に自動で動き、だれもサインインしていなくても止まらない |
| トレイGUI（`tray_main.py`） | 一般の利用者 | 画面・タスクトレイ・デスクトップへの通知。サービスの内部 API を呼ぶだけで、自分では監視・スキャンをしない |
| 昇格ヘルパー（`elevate_main.py`） | 管理者 / root | 管理者の操作（保護の設定の変更・隔離の復元と削除・clamd の開始と停止など）を、確認画面で内容を見せてから実行する |
| 管理用コマンド（`clamqtpie_ctl.py`） | 管理者 / root | 状態の表示、保護された設定の変更、トレイGUI を使える利用者、旧版からの移行、WebUI の設定 |
| 通知のクリックの補助（`notify_activate.py`、Windows） | 一般の利用者 | 通知をクリックしたときに、トレイGUI に開く画面を知らせる |

- トレイGUI を使えるのは、ClamQtPie Users（Windows）・`clamqtpie`（Linux）のグループに入っている利用者だけ
- 設定・データの置き場所はインストーラ/パッケージング設計書 10.4 節

## 7.2 画面一覧（1 章の置き換え）

トレイGUI は 10 画面（左のアイコンの並びの順）。WebUI もほぼ同じ画面を持つ（画面設計書 12 章）。

| 画面 | 概要 |
|---|---|
| ホーム | 保護の状態（clamd・定義・リアルタイム保護・最後のスキャン）、直近のアクティビティ |
| ウイルスと脅威の防止（スキャン） | クイック・フル・場所を指定したスキャン、進み具合、見つかったものの隔離、スキャン履歴 |
| セキュリティ インテリジェンス（定義更新） | 手動の更新、更新の履歴、Linux では OS の定義更新サービス（clamav-freshclam）の状態と開始・停止 |
| 保護の履歴（ログ） | 絞り込み・CSV 出力・消去 |
| 隔離されたアイテム | 隔離の一覧、復元・削除 |
| ClamAV エンジン サービス | clamd の状態と開始・停止 |
| リアルタイム保護 | ON/OFF、検知したときの動作、監視するフォルダ、除外 |
| スキャン スケジューラー | スキャン・定義更新のジョブの追加・編集・削除・今すぐ実行、スリープ中でも起こして実行する |
| システム診断 | 環境の確認、サービスの状態、ヘルスチェック |
| 設定 | 通知・メール・スキャン・定義更新・ログなど（保護された項目は読み取りだけ） |

## 7.3 画面遷移（2 章の置き換え）

``` text
メイン画面（左のアイコンで 10 画面を切り替える）

タスクトレイ
├─ メイン画面を開く
├─ 今すぐスキャン
├─ 定義更新
├─ リアルタイム監視 ON/OFF
├─ 未読の通知（n 件）
├─ WebUI ▸（状態、ブラウザで開く、ON にする（時間）、OFF にする時刻を変更、OFF にする、
│           ログイン設定、二段階認証の設定、ログインの停止を解く）
└─ 終了（トレイGUI だけを終わらせる。サービスの保護は続く）

通知のクリック（Windows）→ その通知が示す画面（隔離など）
```

## 7.4 モジュール構成（3 章・6 章の置き換え）

6 章の `core/`・`db/`・`gui/` の構成に、次を足した。`app.py`（1 つのアプリの起動と結線）は削除した（段階 12-4e）。

``` text
service_main.py            サービスの入口（Windows サービス・systemd・--console）
tray_main.py               トレイGUI の入口
elevate_main.py            昇格ヘルパー
clamqtpie_ctl.py           管理用コマンド
notify_activate.py         通知のクリックの補助（Windows）
build_release.py           配布物の作成（Windows: インストーラ、Linux: tar.gz）
core/
  runtime.py               サービスの部品の組み立てと結線
  service_paths.py         サービスの設定・データ・ログの置き場所
  service_access.py        トレイGUI を使える利用者（グループ）
  service_endpoint.py      内部 API の接続先（endpoint.json）と、相手がサービスかの確認
  peer_verify.py / peer_identity.py  clamd・内部 API の相手の確認、接続元の利用者
  secret_store.py          サービスの秘密（SMTP パスワード）の保存先
  notification_store.py    サービスが出した通知の保存（未読・既読）
  scan_history_service.py  スキャン履歴
  portable_migration.py    旧版（ポータブル）からの移行
  windows_setup.py         Windows の導入・削除（clamqtpie-ctl setup）
  freshclam_service.py     OS の定義更新サービス（Linux）の開始・停止
  wake_timer.py / resume_watcher.py / sleep_inhibit.py  スリープからの復帰・起こす・防ぐ
  web_password.py / totp.py / local_ca.py / web_lan.py / net_scope.py  WebUI のログイン・二段階認証・HTTPS・LAN
  gui_config.py            利用者ごとのトレイGUI の設定（gui.json）
api/                       Web API（FastAPI）
  app.py / server.py / service_api.py   アプリの組み立て・待ち受け（uvicorn）・サービスへの組み込み
  auth.py / web_sessions.py             トークン（内部 API）・ログインのセッション（WebUI）
  routes/                  読み取り・書き込み・ジョブ・通知・WebUI のログイン
  actions.py / jobs.py / events.py      変更の処理・スキャンと定義更新の実行・イベント（SSE）
  web_control.py / web_static.py        WebUI の ON/OFF・画面のファイルを配る
api_client/                トレイGUI のクライアント
  client.py / events.py / sync.py       要求・イベントの受信・画面からの同期的な呼び出し
  proxies.py               サービスと同じ形のプロキシ（画面はこれを使う）
  elevation.py / admin_ops.py           昇格ヘルパーの起動と受け渡し・管理者の操作の一覧と説明
gui/
  screens/                 10 画面（engine.py・diagnostics.py を追加）
  pending_edits.py         保存前の変更のまとめ（リアルタイム保護・スケジュール）
  tray_notifications.py / notification_dialog.py  サービスの通知の表示・未読の一覧
  webui_menu.py / dialogs/webui_dialogs.py        トレイの「WebUI」サブメニュー
web/                       WebUI（index.html・app.js・style.css。ビルドの工程なし）
packaging/                 Windows のインストーラ（Inno Setup）、Linux の install.sh・unit・polkit のポリシー
alembic/                   マイグレーション（0001〜0007）
```

## 7.5 DB 設計（4 章の追加）

4 章・6 章の `quarantine`・`schedule`・`exclude`・`watch_target`・`update_history` に加え、次の表がある（DB マイグレーション設計書と README_DEV の各節）。DB はサービスだけが開く。

| 表 | 内容 | 追加した版 |
|---|---|---|
| `scan_history`・`scan_history_detection`・`scan_history_error` | スキャン履歴（実行元・対象・件数・検知したファイル・検査できなかったファイル） | 0004 |
| （`quarantine` の列の追加） | 隔離ファイルの保存形式（`encoding`）、元の所有者・アクセス権（`owner_info`）、UTF-8 として正しくない元の名前（`original_path_raw`） | 0005 |
| `pending_notification` | サービスが出した通知（未読・既読） | 0006 |
| （`schedule.wake`） | スリープ中でも起こして実行するか | 0007 |
