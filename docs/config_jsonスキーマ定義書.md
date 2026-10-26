# config.json スキーマ定義書

対象: アプリケーション設定ファイル `config.json`
前提: 要件定義書6章「設定管理」、基本設計書4章、および各詳細設計書（clamdプロトコル設計書1.1節、Windowsリアルタイム監視詳細設計書8章、タスクトレイ常駐アプリプロセス設計書8.1節）で登場した設定項目を統合する。

------------------------------------------------------------------------

## 1. 確認事項の反映結果

| 項目 | 決定内容 |
|---|---|
| SMTPパスワード保存 | config.jsonには保存しない。OSキーチェーン連携（Windows: Credential Manager／Linux: Secret Service（`keyring`ライブラリ経由））に保存し、config.jsonにはキーチェーン参照用のアカウント名のみを保持する |
| 保存場所 | 実行ファイルと同一ディレクトリに配置するポータブル構成（例: `<exeの場所>/config.json`） |
| スキーマバージョン管理 | `schema_version`キーのみ付与。マイグレーション処理の仕組み化は今回は行わない（将来対応） |

------------------------------------------------------------------------

## 2. ファイル配置・アクセス方針

### 2.1 配置場所

- 実行ファイル（PyInstaller配布物のexe、またはPythonスクリプト実行時はプロジェクトルート）と同一ディレクトリに`config.json`を配置する
- 関連ディレクトリも同様にポータブル構成とする（既存の基本設計書3章モジュール構成`config/`, `logs/`, `quarantine/`と整合）

```text
<インストール先>/
  ClamAVFrontend.exe
  config.json
  config/            # 予備領域（将来のプロファイル分割等に備え保持、初版は未使用）
  logs/
  quarantine/
  clamonacc/         # Linux: clamonaccに渡す監視対象/除外リストとclamonaccのログ（Linuxリアルタイム監視詳細設計書4章）
```

- Linuxの多重起動防止用ロックファイル（`app.lock`）は、アプリのフォルダではなくユーザー単位の`~/.local/share/clamav-frontend/app.lock`（`XDG_DATA_HOME`があれば従う）に置く（タスクトレイ常駐アプリプロセス設計書3.1節。Phase 11で実装を設計書に合わせた）。Windowsはnamed mutexのためファイルを使わない

### 2.2 注意事項（ポータブル構成に伴う制約）

- 複数の異なる実行環境（別PC等）へ`config.json`ごとコピーして持ち歩く運用が可能になる一方、SMTPパスワードはOSキーチェーンに紐付くため**移動先では再入力が必要**になる。この制約を設定画面のSMTPタブに注記として表示する
- 実行ファイルが読み取り専用の場所（Program Files等、管理者権限必須の場所）に配置された場合、config.json書き込みに失敗する可能性がある。書き込み失敗時はエラーダイアログ「設定を保存できませんでした。実行ファイルの配置場所の書き込み権限を確認してください」を表示する（インストーラ/パッケージング設計書側でインストール先の既定を検討する際の考慮事項として申し送る）

### 2.3 読み込み・書き込みルール

- 起動時: ファイルが存在しない場合は全項目を既定値で新規作成する。存在する場合は読み込み、欠損キーがあれば既定値で補完して上書き保存する（4章の各既定値を参照）
- 保存時: 一時ファイル（`config.json.tmp`）へ書き込み後にリネームするアトミック書き込み方式とし、書き込み中のクラッシュによるファイル破損を防ぐ
- 排他制御: 設定画面からの保存時のみ書き込みを行う（他モジュールは読み取り専用でメモリ上の設定オブジェクトを参照する。設計は`core/config_service.py`相当に集約）

------------------------------------------------------------------------

## 3. SMTPパスワードのキーチェーン連携

### 3.1 保存方式

- Pythonの`keyring`ライブラリを使用（Windows: `keyring.backends.Windows.WinVaultKeyring`、Linux: `keyring.backends.SecretService.Keyring`）
- サービス名: `ClamAVFrontend`
- アカウント名: config.json内`smtp.keyring_account`（既定値`smtp_default`、複数SMTP設定は初版では非対応のため固定値でよい）
- 保存: `keyring.set_password("ClamAVFrontend", account, password)`
- 取得: `keyring.get_password("ClamAVFrontend", account)`（NotificationService利用時に都度取得、メモリ上にキャッシュしない）

### 3.2 Linux環境での留意事項

- Secret Serviceが利用できない環境（GUIログインセッションがない、軽量デスクトップ環境等）では`keyring`がエラーとなる場合がある
- その場合はエラーをキャッチし、設定画面で「この環境ではパスワードの安全な保存に対応していません。SMTP通知機能は利用できません」という警告を表示し、平文保存へのフォールバックは行わない（安全性を優先）

------------------------------------------------------------------------

## 4. スキーマ全体定義

### 4.1 トップレベル構造

```json
{
  "schema_version": 1,
  "general": { },
  "clamd": { },
  "realtime": { },
  "smtp": { },
  "notification": { },
  "update": { },
  "logging": { }
}
```

`schema_version`は整数。今回はマイグレーション処理を実装しないが、将来のバージョンアップ時に「現在のバージョンと異なる場合は警告ログを出す」程度の簡易チェックのみ組み込む。

### 4.2 general（一般設定）

| キー | 型 | 既定値 | 説明 | 出典 |
|---|---|---|---|---|
| autostart | bool | true | OS起動時自動起動 | 要件定義7章、タスクトレイ設計書6章 |
| start_minimized | bool | true | 起動時にウィンドウを表示せずトレイのみで開始するか | タスクトレイ設計書8.1節 |
| minimize_to_tray | bool | true | ×ボタン押下時にトレイへ最小化（常にtrue固定、UIでの変更は不可とし将来拡張用に項目のみ保持。Phase 11で画面設計書をこちらに統一） | タスクトレイ設計書4章 |
| show_tray_hint_shown | bool | false | トレイ格納時の案内吹き出しを表示済みか。設定画面「×ボタンでトレイに格納したときに案内を表示」をONにして保存するとfalseに戻る（タスクトレイ設計書4.1節「設定でこの案内自体をOFFにできる」） | タスクトレイ設計書8.1節 |
| language | string | "ja" | 表示言語（保留機能のため初版はja固定、UI非表示） | 要件定義8章 |

### 4.3 clamd（clamd接続設定）

| キー | 型 | 既定値 | 説明 |
|---|---|---|---|
| connection_type | string | "auto" | "auto" \| "unix" \| "tcp" |
| unix_socket_path | string | "/var/run/clamav/clamd.ctl" | Linux用 |
| tcp_host | string | "127.0.0.1" | Windows用 |
| tcp_port | int | 3310 | Windows用 |
| connect_timeout_sec | int | 5 | |
| read_timeout_sec | int | 30 | |
| instream_chunk_size | int | 8192 | バイト |
| max_stream_size_mb | int | 100 | |
| max_concurrent_connections | int | 4 | clamd.conf側MaxThreadsと整合させる運用注意あり。オンデマンドスキャンはこの値-1までしか使わず、リアルタイム監視用に1接続を常に残す（clamdソケット通信プロトコル設計書5章） |
| auto_start | bool | true | clamdの停止を検知したら、OSのサービスとして起動を1回試みる（Phase 11追加、clamdソケット通信プロトコル設計書9章） |
| service_name | string | "" | 起動するclamdサービス名。空ならOS既定（Windows: "clamd" / Linux: "clamav-daemon"）（Phase 11追加） |

出典: clamdソケット通信プロトコル設計書1.1節、5章

設定画面8-7（「ClamAV エンジン」タブ、コードレビュー後に追加）で connection_type / unix_socket_path / tcp_host / tcp_port / auto_start / service_name を変更できる。接続の設定（connection_type / unix_socket_path / tcp_host / tcp_port、およびタイムアウト・同時接続数等）はアプリの再起動後に反映し、auto_start / service_name は保存直後から使う。設定画面で tcp_host / unix_socket_path を空欄のまま保存した場合は既定値に戻す。

### 4.4 realtime（リアルタイム監視設定）

| キー | 型 | 既定値 | 説明 |
|---|---|---|---|
| default_enabled | bool | false | アプリ起動時に自動でリアルタイム監視を開始するか（監視対象0件の場合は起動しない制約あり） |
| default_action | string | "quarantine" | "quarantine" \| "notify_only" |
| windows_debounce_sec | float | 2.0 | |
| scan_worker_threads | int | 2 | |
| max_pending_timers | int | 1000 | |
| lock_retry_wait_sec | float | 1.0 | |
| linux_backend | string | "clamonacc" | Linux専用。"clamonacc" \| "watchdog"。clamonacc/pkexecが無い環境ではwatchdogへ自動フォールバック（Phase 11で追加） |
| clamonacc_path | string | "clamonacc" | Linux専用。clamonaccの実行ファイル名またはフルパス。設定画面8-7で指定できる（空欄で保存すると"clamonacc"）。次にリアルタイム監視を開始したときから使う |
| clamonacc_config_file | string | "" | Linux専用。clamonaccに渡すclamd.confのパス。空ならclamonaccの既定 |
| clamonacc_fdpass | bool | true | Linux専用。trueかつclamd.confに`LocalSocket`がある場合はclamonaccに`--fdpass`（ファイルディスクリプタ渡し）、それ以外は`--stream`（ファイルの内容を送る）を指定する（Phase 11改訂） |

出典: Windowsリアルタイム監視詳細設計書8章、画面設計書3章

補足: 監視対象パス自体（`watch_target`）と除外リスト（`exclude`）はconfig.jsonではなくSQLiteで管理するため、本スキーマには含めない。

### 4.5 smtp（メール通知接続設定、パスワード除く）

| キー | 型 | 既定値 | 説明 |
|---|---|---|---|
| host | string | "" | |
| port | int | 587 | |
| user | string | "" | |
| keyring_account | string | "smtp_default" | 3章参照、パスワード自体はここに含まない |
| from_address | string | "" | |
| to_address | string | "" | |
| encryption | string | "starttls" | "none" \| "ssl" \| "starttls" |

出典: 画面設計書8-2節

### 4.6 notification（通知条件設定）

| キー | 型 | 既定値 | 説明 |
|---|---|---|---|
| on_detect_mail | bool | true | 検知時メール通知 |
| on_detect_desktop | bool | true | 検知時デスクトップ通知 |
| on_update_fail | bool | true | 定義更新失敗時通知 |
| on_job_fail | bool | true | スケジュールジョブ失敗時通知 |

出典: 画面設計書8-3節

### 4.7 update（定義更新設定）

| キー | 型 | 既定値 | 説明 |
|---|---|---|---|
| auto_update | bool | true | 自動更新の有効/無効 |
| interval_hours | int | 4 | 自動更新の実行間隔 |
| freshclam_path | string | "freshclam" | freshclamの実行ファイル名（PATHから探す）またはフルパス。設定画面8-5で指定する（コードレビューで追加。PATHに無い環境向け） |

出典: 画面設計書8-5節

### 4.8 logging（ログ設定）

| キー | 型 | 既定値 | 説明 |
|---|---|---|---|
| retention_days | int | 90 | 30/60/90/180/365のいずれかのみ許容（バリデーション） |
| level | string | "INFO" | "DEBUG" \| "INFO" \| "WARNING" \| "ERROR" \| "CRITICAL" |

出典: 要件定義5章・7章、画面設計書8-6節

------------------------------------------------------------------------

## 5. バリデーションルール

| キー | ルール | 違反時の挙動 |
|---|---|---|
| clamd.tcp_port | 1〜65535の整数 | 設定画面で保存時に入力エラー表示、保存不可 |
| clamd.max_concurrent_connections | 1以上の整数 | 同上 |
| realtime.windows_debounce_sec | 0.1〜10.0の範囲 | 同上 |
| realtime.linux_backend | 許容値（clamonacc/watchdog）以外は"clamonacc"へ自動補正 | 読込時WARNINGログ |
| smtp.port | 1〜65535の整数 | 同上 |
| smtp.encryption | 許容値以外は"starttls"にフォールバック | 読込時に自動補正しWARNINGログ |
| logging.retention_days | 許容値（30/60/90/180/365）以外はセレクトUIでは選択不可 | 直接ファイル編集で不正値が入った場合は読込時に90へ自動補正しWARNINGログ |
| logging.level | 許容値以外は"INFO"へ自動補正 | 読込時WARNINGログ |
| update.freshclam_path | 空文字・文字列以外は"freshclam"へ自動補正（設定画面で空欄のまま保存した場合も"freshclam"） | 読込時WARNINGログ |
| schema_version | 未来のバージョン番号（アプリの想定より大きい）を検出した場合 | 「新しいバージョンで作成された設定ファイルの可能性があります」の警告ログのみ（読込は継続、上書き保存はしない＝互換性のないキーを消さないための保護） |

------------------------------------------------------------------------

## 6. 完全なサンプルconfig.json

```json
{
  "schema_version": 1,
  "general": {
    "autostart": true,
    "start_minimized": true,
    "minimize_to_tray": true,
    "show_tray_hint_shown": false,
    "language": "ja"
  },
  "clamd": {
    "connection_type": "auto",
    "unix_socket_path": "/var/run/clamav/clamd.ctl",
    "tcp_host": "127.0.0.1",
    "tcp_port": 3310,
    "connect_timeout_sec": 5,
    "read_timeout_sec": 30,
    "instream_chunk_size": 8192,
    "max_stream_size_mb": 100,
    "max_concurrent_connections": 4,
    "auto_start": true,
    "service_name": ""
  },
  "realtime": {
    "default_enabled": false,
    "default_action": "quarantine",
    "windows_debounce_sec": 2.0,
    "scan_worker_threads": 2,
    "max_pending_timers": 1000,
    "lock_retry_wait_sec": 1.0,
    "linux_backend": "clamonacc",
    "clamonacc_path": "clamonacc",
    "clamonacc_config_file": "",
    "clamonacc_fdpass": true
  },
  "smtp": {
    "host": "",
    "port": 587,
    "user": "",
    "keyring_account": "smtp_default",
    "from_address": "",
    "to_address": "",
    "encryption": "starttls"
  },
  "notification": {
    "on_detect_mail": true,
    "on_detect_desktop": true,
    "on_update_fail": true,
    "on_job_fail": true
  },
  "update": {
    "auto_update": true,
    "interval_hours": 4,
    "freshclam_path": "freshclam"
  },
  "logging": {
    "retention_days": 90,
    "level": "INFO"
  }
}
```

------------------------------------------------------------------------

## 7. ConfigService インターフェース（core/config_service.py 相当）

```text
ConfigService
  - load() -> ConfigModel        # 起動時1回、欠損キー補完・バリデーション込み
  - save(config: ConfigModel)    # アトミック書き込み（3.3節の一時ファイル方式）
  - get_smtp_password() -> str | None   # keyring経由取得
  - set_smtp_password(password: str)    # keyring経由保存
  - delete_smtp_password()              # keyring経由削除（SMTP設定クリア時）
```

- 他モジュール（ClamAVService、RealtimeManager等）は起動時に`ConfigService.load()`済みの`ConfigModel`（イミュータブルなデータクラス想定）を注入されて参照する
- 設定画面で保存された場合、変更を要するサービス（自動起動、ログ保持期間、リアルタイム監視パラメータ等）へは`ConfigService`からのシグナル発火で再読込を通知する（タスクトレイ常駐アプリプロセス設計書7章のシグナル/スロット方針に準拠）

------------------------------------------------------------------------

## 8. 未確定事項（次工程で決定が必要）

1. `keyring`ライブラリがLinux環境で利用不可の場合の代替手段（3.2節、SMTP通知機能自体を諦める方針でよいか運用者側の合意が必要）
2. ポータブル構成による書き込み権限エラー時の具体的な救済導線（インストール先の見直しを促すか、別ディレクトリへの保存を提案するか）はインストーラ/パッケージング設計書で確定させる
3. `config/`ディレクトリ（現状予備領域）の用途を将来的にどう使うか（プロファイル分割等）は今回スコープ外
