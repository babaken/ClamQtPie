# ClamAVフロントエンド 要件定義（改訂版）

## 1. ClamAVとの連携方式

-   `clamd`デーモンへソケット接続する方式を採用する。
-   `clamscan`は通常運用では使用せず、障害解析時のみ利用する。

### 理由

-   高速スキャン
-   リアルタイム監視との親和性
-   Windows/Linux共通実装

------------------------------------------------------------------------

## 2. Windowsでのリアルタイム監視

### Linux

-   `clamonacc`を利用
-   `clamd`と連携

### Windows

-   `clamonacc`は利用不可
-   `watchdog` + `ReadDirectoryChangesW`
-   検知したファイルを`clamd`へ送信

### 動作形態

-   Windowsサービス化しない
-   タスクトレイ常駐アプリとして動作

------------------------------------------------------------------------

## 3. 隔離機能

### 保存

    Quarantine/
        UUID.dat

### アクセス制御

-   読み取り専用
-   実行不可権限
-   UUIDへリネーム

### メタデータ管理

SQLiteを採用

### テーブル例

``` sql
CREATE TABLE quarantine (
    id INTEGER PRIMARY KEY,
    uuid TEXT,
    original_path TEXT,
    virus_name TEXT,
    sha256 TEXT,
    size INTEGER,
    detected_at TEXT,
    restored INTEGER,
    deleted INTEGER
);
```

------------------------------------------------------------------------

## 4. スケジュール機能

-   APScheduler採用
-   SQLiteへジョブ情報保存

``` sql
CREATE TABLE schedule (
    id INTEGER PRIMARY KEY,
    name TEXT,
    enabled INTEGER,
    cron TEXT,
    target TEXT,
    scan_type TEXT,
    last_run TEXT,
    next_run TEXT
);
```

------------------------------------------------------------------------

## 5. ログ管理

Python標準`logging`を利用

-   TimedRotatingFileHandler
-   デフォルト保持期間：90日
-   設定変更可能

------------------------------------------------------------------------

## 6. 設定管理

設定ファイルは`config.json`

保存対象

-   SMTP
-   UI設定
-   通知設定
-   リアルタイム監視
-   更新設定

------------------------------------------------------------------------

## 7. 初版実装機能

### 起動

-   OS起動時自動起動
-   タスクトレイ常駐

### 除外リスト

-   ファイル
-   フォルダ
-   拡張子
-   プロセス

SQLite管理

``` sql
CREATE TABLE exclude (
    id INTEGER PRIMARY KEY,
    type TEXT,
    value TEXT,
    enabled INTEGER
);
```

### ログローテーション

-   初期値90日
-   30/60/90/180/365日に変更可能

------------------------------------------------------------------------

## 8. 保留機能

-   パスワード付きZIP対応
-   アーカイブ深度設定
-   CPU使用率制限
-   ダークモード
-   フロントエンド自己更新
-   多言語対応

------------------------------------------------------------------------

# データ管理方針

  データ         保存方式       理由
  -------------- -------------- ----------------------------------
  設定           JSON           編集・バックアップが容易
  隔離情報       SQLite         検索・復元・一覧表示に適する
  スケジュール   SQLite         APSchedulerとの親和性
  除外リスト     SQLite         GUI操作が容易
  スキャンログ   ログファイル   大量データ・ローテーションに強い
  隔離ファイル   ファイル       DB格納不要

------------------------------------------------------------------------

# システム構成

``` text
GUI (PySide6)
    │
    ▼
Frontend Controller
    │
    ├── ClamAVService
    │      ├── start_clamd()
    │      ├── stop_clamd()
    │      ├── scan_file()
    │      ├── scan_directory()
    │      ├── update_database()
    │      ├── start_realtime()
    │      └── stop_realtime()
    │
    ├── APScheduler
    ├── SQLite
    ├── Logging
    └── Notification
            │
            ▼
         clamd
            │
    ┌───────┴────────┐
    │                │
Linux          Windows
clamonacc      watchdog
```
