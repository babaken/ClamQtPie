# 開発環境セットアップ（Phase 0）

実装ステップ計画書 Phase 0 に対応する開発環境構築手順とチェック結果。

## 1. ディレクトリ構成

```text
clamav_frontend/
  core/               # サービス層（ClamAVService, RealtimeManager, ConfigService等をPhase1以降で実装）
  db/
    models/           # テーブル対応データクラス
  gui/                # 画面（Phase3以降）
  alembic/
    versions/         # マイグレーションスクリプト（Phase1で0001_initial追加）
  tests/
    unit/
    integration/
    fixtures/         # EICARテストファイル等
    conftest.py       # 共通fixture
  icons/
  manual/
  requirements.txt
  README_DEV.md        # 本ファイル
```

## 2. Python仮想環境・依存パッケージ

```bash
python3 -m venv .venv
. .venv/bin/activate          # Windowsは .venv\Scripts\activate
pip install -r requirements.txt
```

### インストール確認済みパッケージ（本環境で検証済み）

| パッケージ | バージョン | 用途 |
|---|---|---|
| PySide6 | 6.11.1 | GUI |
| SQLAlchemy | 2.0.51 | DB ORM |
| alembic | 1.18.5 | DBマイグレーション |
| APScheduler | 3.11.3 | スケジュール機能 |
| keyring | 25.7.0 | SMTPパスワードのOSキーチェーン連携 |
| watchdog | 6.0.0 | Windowsリアルタイム監視 |
| pyinstaller | 6.21.0 | パッケージング |
| pytest / pytest-cov | 9.1.1 / 7.1.0 | テスト |
| freezegun | 1.5.5 | デバウンス・集約ウィンドウのテスト用時間操作 |

Windows専用の`pywin32`は`requirements.txt`内で`sys_platform == "win32"`条件付きにしてあるため、Linux開発機でも`pip install -r requirements.txt`が失敗しない。

## 3. 動作確認結果

| 確認項目 | 結果 |
|---|---|
| `pytest`空スイート実行 | OK（6件のPhase0確認テストが全てpass） |
| PySide6 QApplication起動（`QT_QPA_PLATFORM=offscreen`） | OK |
| EICARテストファイル配置（`tests/fixtures/eicar.txt`） | OK |
| conftest.pyの共通fixture雛形（`eicar_file`, `real_clamd_available`, `requires_real_clamd`マーカー） | OK |
| ClamAV本体（clamd/freshclam）のインストール | **本開発コンテナでは未完了**（後述4章） |

## 4. 既知の制約：本コンテナでのClamAV導入について

本開発用サンドボックス環境ではパッケージミラーの制約により`apt-get install clamav-daemon`が404エラーで失敗し、clamd/freshclamを導入できなかった。

- これは開発コンテナ固有の制約であり、実際の開発機（ユーザーのArchLinux/KDE環境やWindows環境）では通常どおり`pacman -S clamav`や公式インストーラでの導入が可能な想定
- Phase 2（clamd通信コア）着手時に、実開発機上でclamdを導入し、`tests/conftest.py`の`requires_real_clamd`マーカーを使った結合テスト（CLM-I01等）を実行すること
- 本コンテナ環境ではユニットテスト（モックclamdサーバを使うもの）のみ実行可能。実clamd結合テストは開発機側で別途実施する

## 5. Phase 0 完了判定

実装ステップ計画書の完了判定「`pytest`が空のテストスイートでも正常起動すること、clamdへ手動で疎通確認できること」について：

- pytest動作: 達成
- clamd疎通確認: **本コンテナでは未達成**（4章の制約による）。実開発機での確認をPhase 2開始条件とする

## 6. Phase 1 実装状況（土台: config / DB / 例外 / ロギング）

### 実装したモジュール

| モジュール | 内容 | 対応設計書 |
|---|---|---|
| `core/exceptions.py` | AppError基底＋E1000〜E8000番台の例外階層 | エラーコード一覧・例外設計書 |
| `core/config_model.py` | ConfigModelデータクラス群、バリデーション・自動補正 | config.jsonスキーマ定義書4〜5章 |
| `core/config_service.py` | config.json読み書き（アトミック書き込み）、keyring連携 | 同2〜3章 |
| `core/logger.py` | TimedRotatingFileHandler、エラーコード付きログ出力 | エラーコード一覧・例外設計書5.2節 |
| `db/models/__init__.py` | SQLAlchemyモデル（quarantine/schedule/exclude） | DBマイグレーション設計書7章 |
| `alembic/versions/0001_initial.py` | 初期スキーマ作成マイグレーション | 同7章 |
| `db/database.py` | DatabaseService（接続・自動マイグレーション適用） | 同5章 |

### テスト結果

```text
pytest -q --cov=core --cov=db --cov-report=term-missing
24 passed
TOTAL カバレッジ 90%
```

テストケース対応: CFG-U01〜U06, CFG-I01〜I02, DB-U01〜U02, ERR-U01〜U02（テスト仕様書3.5, 3.7, 3.8節）

### 実装時の設計書との差分・補足事項

1. **alembicのfileConfigによるロガー無効化**: Alembicの`env.py`が呼ぶ`logging.config.fileConfig()`は既定で他の既存ロガーを無効化してしまうため、`disable_existing_loggers=False`を明示指定した。DBマイグレーション設計書には記載がなかった実装上の注意点のため、ここに追記する。
2. **CFG-U04（書き込み権限エラー）テスト**: 開発・CI環境がroot権限で動く場合、`chmod`による書き込み制限が効かないことがあるため、`os.replace`を直接失敗させる形でテストを実装した（環境非依存化）。
3. **DBマイグレーションのCLI実行**: `alembic.ini`の`sqlalchemy.url`はプレースホルダのままにし、実行時は`DatabaseService.migrate()`が`alembic_cfg.attributes["connection"]`へ実際のSQLAlchemy Connectionを渡す方式（DBマイグレーション設計書2.2節どおり）で動作確認済み。

### 未実施（Phase 2以降に持ち越し）

- 実clamd環境でのE5002（マイグレーション失敗）以外のエラーコードの実地検証
- `logging.retention_days`に応じた古いログの実削除確認（TimedRotatingFileHandlerのbackupCountに委譲しているが、日付ベースでの実削除は長期稼働後でないと確認できないため、Phase 10の性能検証時に確認）

## 8. Phase 2 実装状況（clamd通信コア）

### 実装したモジュール

| モジュール | 内容 | 対応設計書 |
|---|---|---|
| `core/clamd_client.py` | ClamdClient（低レベルプロトコル層）。接続方式自動判定、PING/VERSION/SCAN/MULTISCAN/INSTREAM/RELOAD、応答パース | clamdソケット通信プロトコル設計書1〜3章 |
| `core/clamav_service.py` | ClamAVService（上位サービス層）。PINGリトライ、自動再起動フック、同時実行数制御（セマフォ） | 同4〜7章 |

### テストfixture

- `tests/conftest.py`に`FakeClamdServer`（モックclamdサーバ）を追加。TCPソケットでPING/VERSION/SCAN/MULTISCAN/INSTREAMの応答を模擬し、`response_map`でテストごとに応答内容を差し替え可能
- `requires_real_clamd`マーカーにより、実clamdが利用できない環境（本コンテナ含む）では結合テストを自動スキップ

### テスト結果

```text
pytest -q --cov=core --cov=db --cov-report=term-missing
37 passed, 1 skipped
TOTAL カバレッジ 88%
```

テストケース対応:
- CLM-U01〜U09（`tests/unit/test_clamd_client.py`）: 全項目実装・pass
- CLM-I02（同時接続数制御、`tests/unit/test_clamav_service.py`）: モックサーバで検証・pass
- CLM-I01（実clamd + EICAR、`tests/integration/test_real_clamd.py`）: 本コンテナでは**スキップ**（4章の制約により実clamd未導入のため）。実開発機での実行が必要

### 実装時の設計書との差分・補足事項

1. **INSTREAMのfakeサーバ実装**: モックclamdサーバは当初「1回recvして即応答」する簡易実装だったため、INSTREAMのストリーム終端（長さ0チャンク）を待たずに応答してBrokenPipeErrorが発生した。ストリーム終端まで読み切ってから応答するよう修正した（設計書のプロトコル仕様どおりの挙動をテスト側でも正しく模擬する必要があった、という実装上の教訓）。
2. **`ensure_available()`の例外処理漏れ**: 自動再起動試行後の再PINGで`ClamdConnectionError`を捕捉していなかったバグを修正。再起動を試みても接続できない場合は静かに`False`を返す仕様（4章）に合わせた。
3. **clamdプロセス起動（`restart_clamd`コールバック）は未実装**: タスクトレイ常駐アプリプロセス設計書6章のスコープであり、Phase 8で実装予定。現時点では`ClamAVService`はコールバックを外部から注入できる形にとどめている。
4. **freshclam連携は未実装**: `update_database()`は`RELOAD`コマンドでclamdに再読込を指示するのみ。実際の定義ファイルダウンロード（freshclam起動）はPhase 6（スケジュール機能）で実装予定。

### 未実施（Phase 5, 8, 6以降に持ち越し）

- 実clamd環境でのCLM-I01実行確認（開発機でclamd導入後に実施）
- clamdプロセスの起動/停止制御（E8003関連）
- freshclamの呼び出し・進捗ストリーム取得（通知機能詳細仕様書・画面設計書6章向け）

## 10. Phase 3 実装状況（GUIスケルトン）

### 実装したモジュール

| モジュール | 内容 | 対応設計書 |
|---|---|---|
| `gui/main_window.py` | MainWindow（左ナビ＋右コンテンツ、ステータスバー、画面遷移、エラーダイアログ連携） | 画面設計書「共通仕様」 |
| `gui/error_dialog.py` | 共通エラーダイアログ（エラーコード表示＋「詳細を見る」→ログ画面遷移）、確認ダイアログ | エラーコード一覧・例外設計書5.1節 |
| `gui/screens/base.py` | 画面共通基底クラス（タイトル表示、Phase4以降のコンテンツ差し込み口） | 画面設計書「共通仕様」 |
| `gui/screens/home.py` | ホーム画面（ステータスカード×3、クイックアクション、アクティビティ一覧） | 画面設計書1章 |
| `gui/screens/scan.py` | スキャン画面（対象選択、進捗、結果一覧） | 同2章 |
| `gui/screens/realtime.py` | リアルタイム画面（監視トグル、対象パス一覧、除外設定タブ×4、検知時アクション） | 同3章 |
| `gui/screens/schedule.py` | スケジュール画面（ジョブ一覧、操作ボタン） | 同4章 |
| `gui/screens/quarantine.py` | 隔離画面（検索、一覧、復元/削除） | 同5章 |
| `gui/screens/update.py` | 定義更新画面（バージョン表示、実行ログ、更新履歴） | 同6章 |
| `gui/screens/log.py` | ログ画面（フィルタ、一覧、CSV出力、`filter_by_error_code()`） | 同7章 |
| `gui/screens/settings.py` | 設定画面（6サブタブ：一般/SMTP/通知/リアルタイム/定義更新/ログ） | 同8章 |
| `app.py` | アプリエントリポイント（Phase3時点は起動のみ、多重起動防止等はPhase8） | タスクトレイ常駐アプリプロセス設計書8章 |

### テスト結果

```text
pytest -q --cov=core --cov=db --cov=gui --cov-report=term-missing
43 passed, 1 skipped
TOTAL カバレッジ 93%
```

`pytest-qt`を追加導入し、`QT_QPA_PLATFORM=offscreen`でGUIテストをヘッドレス実行できるようにした（`tests/conftest.py`に`qapp`/`qtbot`関連の設定済み）。

テスト対応: 8画面すべての登録・表示・遷移確認、ステータスバー3項目の確認、エラーダイアログの共通フォーマット確認、「詳細を見る」→ログ画面遷移＋エラーコード自動入力の確認（テスト仕様書6.1節の一部を自動テスト化）。

### 実装時の設計書との差分・補足事項

1. **左ナビゲーションのラベル統一**: 画面設計書3章の画面タイトルは「リアルタイム監視」だが、当初ナビゲーションラベルを「リアルタイム」と短縮していたため、両者の突き合わせテストで不一致が発覚し「リアルタイム監視」に統一した。ナビゲーションラベルと画面タイトルは今後常に一致させる方針とする。
2. **各画面はPhase3時点ではダミーデータ・未接続**: ウィジェットの配置と型は画面設計書どおりだが、ClamAVService/DatabaseService等との結線はPhase4以降で行う。
3. **エラーダイアログの「詳細を見る」ボタン**: `QMessageBox.addButton()`をActionRoleで追加し、クリック時にMainWindow側でログ画面へ遷移＋`filter_by_error_code()`でエラーコードを検索欄へ自動入力する形で実装した。

### 未実施（Phase4以降に持ち越し）

- 各画面のボタン・入力欄とサービス層（ClamAVService, DatabaseService, ConfigService）の結線
- ×ボタンのトレイ格納挙動、タスクトレイメニュー（Phase8）
- 実際のウィンドウ表示での見た目確認（本コンテナはoffscreenのみのため、実機での目視確認が別途必要）

## 12. Phase 4 実装状況（スキャン機能〜隔離）

### 実装したモジュール

| モジュール | 内容 | 対応設計書 |
|---|---|---|
| `core/quarantine_service.py` | QuarantineService（隔離/復元/完全削除/一覧、DB登録失敗時のロールバック） | 要件定義書3章、画面設計書5章、エラーコード一覧4.3節 |
| `core/exclude_service.py` | ExcludeService（除外リストCRUD、ファイル/フォルダ/拡張子の照合） | 画面設計書3章、Windowsリアルタイム監視詳細設計書4.2節 |
| `core/scan_dispatcher.py` | ScanDispatcher/ScanWorker（QThread上でのバックグラウンドスキャン、進捗シグナル） | 画面設計書2章、Windowsリアルタイム監視詳細設計書6章 |
| `gui/screens/scan.py` | スキャン画面とScanDispatcher/QuarantineServiceの結線 | 画面設計書2章 |
| `gui/screens/quarantine.py` | 隔離画面とQuarantineServiceの結線（一覧・検索・復元・完全削除） | 画面設計書5章 |
| `app.py` | config/DB/clamd/スキャン/隔離サービスの起動時統合 | タスクトレイ常駐アプリプロセス設計書8章 |

### 実装時の重要な設計判断（クラムdプロトコル設計書からの意図的な逸脱）

clamdソケット通信プロトコル設計書2.1節では「ディレクトリはMULTISCANを使用する」とされているが、MULTISCANは全件スキャン完了まで応答が返らないため、画面設計書2章が求める「現在スキャン中のファイルパス」のリアルタイム表示や検知の逐次反映ができない。そのため`ScanDispatcher`はディレクトリ内ファイルを列挙し、1ファイルずつ`scan_file()`（SCAN）を呼び出す方式で実装した。MULTISCANはAPI自体は引き続き提供し、将来的な性能最適化の余地として残している。

### テスト結果

```text
pytest -q --cov=core --cov=db --cov=gui --cov-report=term-missing
73 passed, 1 skipped
TOTAL カバレッジ 91%
```

テストケース対応:
- QT-U01〜U05（`tests/unit/test_quarantine_service.py`）: 全項目実装・pass
- QT-I01（`tests/integration/test_scan_to_quarantine_flow.py`）: 検知→隔離→一覧表示→復元の一連の流れをEICARファイルで確認・pass
- ScanDispatcher/ScanWorker（`tests/unit/test_scan_dispatcher.py`, `test_scan_dispatcher_threaded.py`）: 単発ファイル/ディレクトリ再帰/除外適用/停止フラグ/QThread実行を確認
- GUI結線（`tests/unit/test_scan_screen.py`, `test_quarantine_screen.py`）: ボタン操作とサービス層呼び出しの対応を確認

### 実装中に発見・修正した不具合

1. **結合テストのハング原因**: 統合テスト（QT-I01）作成時、隔離画面の「復元」ボタンをクリックする手順で確認ダイアログ（`show_confirm_dialog`）をモック化し忘れ、実際の`QMessageBox.exec()`がoffscreen環境で応答待ちのまま無限にハングする不具合が発生した。GUIテストで確認ダイアログを伴うボタン操作は必ずモック化する必要がある、という教訓を得た（今後のPhase6, 7のGUIテストでも同様の注意が必要）。
2. **fake_clamd_serverのシャットダウン競合**: テスト終了時に稀に`Bad file descriptor`警告が出ていたため、`_serve_loop()`内の`settimeout()`呼び出しを`try/except`で保護し、`stop()`との競合を解消した。

### 未実施（Phase5以降に持ち越し）

- ホーム画面のクイックアクション（今すぐスキャン等）とサービス層の結線（Phase8のタスクトレイ統合時にまとめて実施）
- クイックスキャン/フルスキャンの対象パス既定値は暫定的にホームディレクトリ（`~`）としている。OS別の正式な対象パスは実装ステップ計画書の未確定事項1のとおり、実機検証と合わせてPhase5以降に確定する
- 除外リストの「プロセス」区分はPhase5（リアルタイム監視、Windowsのみ意味を持つ）まで判定対象外

## 14. Phase 5 実装状況（リアルタイム監視: Windows優先）

### 実装したモジュール

| モジュール | 内容 | 対応設計書 |
|---|---|---|
| `db/models/__init__.py` | WatchTargetモデル追加 | Windowsリアルタイム監視詳細設計書3.2節 |
| `alembic/versions/0002_add_watch_target.py` | watch_targetテーブル追加マイグレーション | DBマイグレーション設計書8章 |
| `core/watch_target_service.py` | WatchTargetService（監視対象パスのCRUD、消失時の無効化） | 同3.2節、エラーコード一覧E2001 |
| `core/realtime_manager.py` | RealtimeManager（watchdog連携、デバウンス、除外照合、スキャンディスパッチ、検知時アクション） | 同全体 |
| `gui/screens/realtime.py` | リアルタイム画面とRealtimeManager/WatchTargetService/ExcludeServiceの結線 | 画面設計書3章 |
| `app.py` | RealtimeManagerの起動時統合、終了時停止処理 | タスクトレイ常駐アプリプロセス設計書8章 |

### 実装上の重要な注記（設計書の前提との差異）

Windowsリアルタイム監視詳細設計書はWindows固有のAPI（`ReadDirectoryChangesW`）を前提に記述されているが、実装では`watchdog`ライブラリを使用したため、OS差異（Windows/Linux inotify）はライブラリ側で吸収される。そのため`RealtimeManager`はOS非依存のコードとして実装でき、本コンテナ（Linux）上でも実際の監視動作を結合テストで検証できた。ただし要件定義書ではLinux側は別途`clamonacc`連携が想定されており、`watchdog`ベースの実装をLinuxでもそのまま採用するか、`clamonacc`へ置き換えるかは未確定事項として残す。

### テスト結果

```text
pytest -q --cov=core --cov=db --cov=gui --cov-report=term-missing
85 passed, 1 skipped
TOTAL カバレッジ 89%
```

テストケース対応:
- RT-U01〜U06（`tests/unit/test_realtime_manager.py`）: 全項目実装・pass
- RT-I01, RT-I02（`tests/integration/test_realtime_flow.py`）: 実際の`watchdog` Observerでファイル作成→検知→自動隔離までを確認、監視対象パス消失時の無効化を確認
- GUI結線（`tests/unit/test_realtime_screen.py`）: トグル操作・対象追加削除・検知時アクション切替の確認

### 実装中に発見・修正した重大な不具合

**`_DebounceManager`内のデッドロック**: `max_pending_timers`超過時に最古のタイマーを即時発火する処理（`_fire()`呼び出し）を、`notify()`が保持する`threading.Lock`の内側から呼んでいたため、`_fire()`内で再度同じロックを取得しようとしてデッドロックが発生していた。`threading.Lock`を`threading.RLock`に変更して解消した。この不具合はRT-U04のテスト作成時に発見され、テストなしでは気づきにくい典型的な並行処理バグだった。

**GUIテストでのクロススレッドシグナル未達**: 結合テスト（RT-I01）で`RealtimeManager.detected`シグナル（ワーカースレッドから発火）を待つ際、`time.sleep()`によるbusy-waitでは受信側スレッド（メインスレッド）のQtイベントループが処理されず、シグナルが永遠に届かなかった。`qtbot.waitSignal()`を使うことで解消した。

### 未実施（Phase7, 8以降に持ち越し）

- 検知時の通知送信（デスクトップ通知・メール）はPhase7で実装。現状は`_on_detected()`が空実装
- 「プロセス」除外区分はExcludeServiceのCRUD自体は用意したが、リアルタイム監視での判定対象には含めていない（Windowsリアルタイム監視詳細設計書4.2節の既知の制限どおり）
- Linux側の最終実装方針（watchdog継続 or clamonacc）の確定
- ホーム画面のリアルタイム監視トグル・ステータス表示との連携（Phase8のタスクトレイ統合時にまとめて実施）

## 16. Phase 6 実装状況（スケジュール機能）

### 実装したモジュール

| モジュール | 内容 | 対応設計書 |
|---|---|---|
| `db/models/__init__.py` | UpdateHistoryモデル追加 | 画面設計書6章、画面設計書9章未確定事項1（本Phaseで確定） |
| `alembic/versions/0003_add_update_history.py` | update_historyテーブル追加マイグレーション | DBマイグレーション設計書 |
| `core/scheduler_service.py` | SchedulerService（APScheduler連携、ジョブCRUD、バリデーションE4001/E4002、実行時エラーE4003） | 画面設計書4章 |
| `core/update_service.py` | UpdateService（freshclam同期実行、履歴記録）＋UpdateDispatcher（GUI用QThreadラッパー） | 画面設計書6章 |
| `gui/dialogs/job_edit_dialog.py` | ジョブ編集ダイアログ（プリセット周期／カスタムCron切替） | 画面設計書4章「ジョブ編集ダイアログ」 |
| `gui/screens/schedule.py` | スケジュール画面とSchedulerServiceの結線 | 画面設計書4章 |
| `gui/screens/update.py` | 定義更新画面とUpdateService/UpdateDispatcherの結線 | 画面設計書6章 |
| `app.py` | SchedulerService/UpdateServiceの起動時統合、config.update.auto_updateに基づく自動更新ジョブ登録 | config.jsonスキーマ定義書4.7節 |

### 実装上の決定事項（設計書の未確定事項を本Phaseで解消）

画面設計書9章「未確定事項1」（定義更新履歴を専用DBテーブルとして持つか、ログから都度集計するか）について、専用テーブル（`update_history`）方式を採用した。ログからの都度集計は検索性能・実装複雑度の面で不利と判断したため。

### テスト結果

```text
pytest -q --cov=core --cov=db --cov=gui --cov-report=term-missing
109 passed, 1 skipped
TOTAL カバレッジ 87%
```

テストケース対応:
- SC-U01〜U04相当（`tests/unit/test_scheduler_service.py`）: ジョブ重複チェック、Cron式バリデーション、即時実行、実行時エラーを確認
- UpdateService（`tests/unit/test_update_service.py`）: freshclam成功/失敗/コマンド未検出（E8003）を確認
- GUI結線（`tests/unit/test_schedule_update_screens.py`, `test_job_edit_dialog.py`）: ジョブ追加/編集/削除/即時実行、更新画面のボタン・ログ・履歴表示を確認

### 実装中に発見・修正した不具合

1. **`textwrap.dedent`とf-string複数行埋め込みの組み合わせバグ**: UpdateServiceのテストで、モック用freshclamスクリプトを生成する際、複数行の`body`文字列をインデント付きf-stringテンプレートへ埋め込んでから`textwrap.dedent()`を適用すると、`body`側の2行目以降がインデント0のまま挿入されるため`dedent`の共通接頭辞計算が壊れ、生成スクリプトの一部だけインデントが残って`IndentationError`となりテストが原因不明のまま失敗し続ける不具合があった。原因特定には標準出力に現れないサブプロセスの終了コード不一致から辿る必要があり、対処療法（リトライ等）ではなく生成方法自体を単純な文字列結合に変更して解消した。テキストテンプレート生成で`dedent`と可変長の埋め込み文字列を組み合わせる際は要注意、という教訓を得た。
2. **DBモデル定義の重複追加**: `UpdateHistory`モデルをファイルへ追記する際、同一クラスが誤って2回定義される事象が発生し、Alembicの自動生成が「テーブルが二重に検出される」形の紛らわしいエラーを出した。ファイル全体を確認してから重複ブロックを削除して解消した。

### 未実施（Phase7以降に持ち越し）

- スケジュールジョブ失敗時の通知送信（`on_job_fail`）はPhase7で実装
- ホーム画面のクイックアクション・定義更新ボタンとの連携（Phase8のタスクトレイ統合時にまとめて実施）
- ジョブ実行中の重複実行防止（同一ジョブが「今すぐ実行」とスケジュール実行で同時に走った場合の排他制御）は明示実装しておらず、必要性を含めて次工程で検討

## 18. Phase 7 実装状況（通知機能）

### 実装したモジュール

| モジュール | 内容 | 対応設計書 |
|---|---|---|
| `core/detection_aggregator.py` | DetectionAggregator（60秒固定ウィンドウでの検知集約、50件打ち切り） | 通知機能詳細仕様書3章 |
| `core/notification_service.py` | NotificationService（デスクトップ通知: Windows PowerShell Toast/Linux notify-send、メール通知: プレーンテキスト、リトライなし） | 同4〜5章 |
| `core/scheduler_service.py` | `on_job_fail`コールバック追加（ジョブ失敗時の通知フック） | 同2.1節 |
| `gui/screens/settings.py` | 設定画面をConfigService/NotificationServiceと結線（保存/読込、SMTPテスト送信） | 画面設計書8-2節 |
| `app.py` | RealtimeManager.detected / ScanDispatcher.file_scanned / SchedulerServiceのon_job_fail / UpdateServiceの失敗時、それぞれNotificationServiceへ接続 | 通知機能詳細仕様書6章シーケンス |

### 実装上の決定事項

- オンデマンドスキャン（`ScanDispatcher.file_scanned`）のFOUND検知も、リアルタイム監視と同様にNotificationServiceへ通知する。ただしオンデマンドスキャンは画面設計書2章どおり手動隔離のため、`quarantined=False`で通知する（後から手動隔離しても再通知はしない）
- `SchedulerService`はQObjectではなくコールバック注入方式のままとし、`on_job_fail`コールバックを追加する形で最小限の変更にとどめた（Phase6で構築した設計を壊さないため）

### テスト結果

```text
pytest -q --cov=core --cov=db --cov=gui --cov-report=term-missing
124 passed, 1 skipped
TOTAL カバレッジ 87%
```

テストケース対応:
- NT-U01〜U02（`tests/unit/test_detection_aggregator.py`）: 60秒集約・50件打ち切りを確認
- NT-U03〜U05相当（`tests/unit/test_notification_service.py`）: メール本文テンプレート、SMTP未設定時のスキップ、送信失敗時のリトライなし、デスクトップ通知（notify-send）呼び出し、on_update_fail設定のON/OFFを確認
- 設定画面結線（`tests/unit/test_settings_screen.py`）: 読込/保存/SMTPパスワードのkeyring連携/テスト送信の成功・失敗を確認

### 実装中に発見・修正した不具合

1. **メール本文検証テストのBase64デコード漏れ**: `MIMEText`は日本語件名・本文をBase64エンコードするため、送信されたメッセージ文字列を単純な部分一致で検証しようとして失敗した。`email.message_from_string()`で正しくパースし、`get_payload(decode=True)`でデコードしてから検証する方式に修正した。
2. **設定画面のテスト送信ボタンでのダイアログブロッキング**: Phase4・Phase5で発見した教訓（確認ダイアログのモック化漏れ）と同種の不具合が、`QMessageBox.information()`の成功通知ダイアログでも再発した。GUIテストでは操作の結果表示されるダイアログもすべてモック化する必要がある、という教訓を改めて確認した。

### 未実施（Phase8以降に持ち越し）

- Windows Toastの実機動作確認（本コンテナはLinuxのため`notify-send`経路のみ実地確認、PowerShell経路はコード上の実装のみ）
- ホーム画面・タスクトレイのアイコン状態（検知あり等）とNotificationServiceの連携はPhase8で実施
- メール本文の`quarantine_status_summary`は簡易実装（直近1回分の隔離状況のみ保持）のため、集約バッチ内で隔離結果が混在するケース（一部は自動隔離成功、一部失敗等）の詳細な出し分けは今後の改善余地として残す

## 20. Phase 8 実装状況（タスクトレイ・プロセス統合）

### 実装したモジュール

| モジュール | 内容 | 対応設計書 |
|---|---|---|
| `core/single_instance.py` | SingleInstanceLock（Windows named mutex／Linux flock） | タスクトレイ常駐アプリプロセス設計書3章 |
| `core/autostart_service.py` | AutostartService（Windowsレジストリ Runキー／Linux XDG Autostart） | 同6章 |
| `core/log_reader_service.py` | LogReaderService（ログ読込・フィルタ・CSV出力、エラーコード帯からのカテゴリ推定） | 画面設計書7章 |
| `gui/tray.py` | TrayIcon（メニュー5項目、状態別アイコン切替、ダブルクリックでメイン画面表示） | タスクトレイ常駐アプリプロセス設計書5章 |
| `gui/main_window.py` | closeEvent実装（×ボタンでトレイ格納、初回のみ吹き出し通知） | 同4章 |
| `gui/screens/log.py` | LogReaderServiceとの結線（フィルタ・CSV出力） | 画面設計書7章 |
| `gui/screens/home.py` | クイックアクション・ステータス表示の結線 | 画面設計書1章 |
| `app.py` | 起動シーケンス10ステップ（多重起動チェック→...→自動起動反映→表示制御）を統合 | タスクトレイ常駐アプリプロセス設計書8章 |

### テスト結果

```text
pytest -q --cov=core --cov=db --cov=gui --cov-report=term-missing
157 passed, 1 skipped
TOTAL カバレッジ 86%
```

テストケース対応:
- 多重起動防止（`tests/unit/test_single_instance.py`）: Linux flock方式で2重取得の失敗・解放後の再取得を確認
- 自動起動（`tests/unit/test_autostart_service.py`）: Linux XDG Autostartファイルの作成/削除、Windowsレジストリ呼び出しをモックで確認
- タスクトレイ（`tests/unit/test_tray.py`）: メニュー構成、状態優先順位（エラー>検知あり>監視中>通常）、ダブルクリック動作
- ×ボタン挙動（`tests/unit/test_main_window_tray.py`）: トレイ格納・吹き出し通知の初回のみ表示・トレイからの再表示
- ログ画面（`tests/unit/test_log_reader_service.py`, `test_log_screen.py`）: 読込・フィルタ・CSV出力
- ホーム画面（`tests/unit/test_home_screen.py`）: ステータス表示・クイックアクションの結線

### 実装上の設計判断

- Windows専用API（`win32event`, `winreg`等）は本コンテナ（Linux）で実行できないため、`import`失敗時のフォールバック（多重起動防止はスキップして起動継続、テストはモック化）を用意した。実際のWindows環境での動作確認は別途必要
- ログ画面のカテゴリ列は、要件定義・画面設計書が想定する「スキャン／リアルタイム／更新／隔離／システム」の分類に対し、実装のロガーが単一の`clamav_frontend`ロガーのみのため、エラーコード帯（E1000=スキャン、E2000=リアルタイム等）からの推定表示とした。コード無しのINFOログ等はすべて「システム」に分類される

### 実起動確認

app.pyを別ディレクトリにコピーして実行し、以下を確認：
- 多重起動防止ロックファイル（`app.lock`）が生成される
- 0001→0002→0003のマイグレーションが連鎖適用される
- clamd未起動環境でもクラッシュせず、E1001警告ログを出しつつ起動完了する（3.2節の可用性要件どおり）
- トレイアイコン画像未配置による警告が出るが起動は継続する（アイコン素材はインストーラ/パッケージング設計書側で用意予定）

### 未実施（Phase9以降に持ち越し）

- トレイアイコン画像素材（`icons/tray_*.png`）の用意はPhase9（パッケージング）で実施
- Windows実機でのUACプロンプト・レジストリ自動起動・PowerShell Toast通知の動作確認
- Linux側のclamonacc連携方針の最終確定（Phase5から継続する未確定事項）

## 22. Phase 9 実装状況（パッケージング）

### 実装したファイル

| ファイル | 内容 | 対応設計書 |
|---|---|---|
| `ClamAVFrontend.spec` | PyInstaller specファイル（--onedir、uac_admin=True、UPX不使用、hiddenimports） | インストーラ/パッケージング設計書3章 |
| `version_info.txt` | Windows実行ファイルのバージョンリソース | 同3.2節 |
| `build_release.py` | ビルド＋Zip作成スクリプト（`python build_release.py --version 1.0.0`） | 同2章 |
| `README.md` / `README_JA.md` | 配布物同梱README（動作要件、UAC注意事項、ポータブル構成、アンインストール、バージョンアップ手順） | 同2章・6章・7章 |
| `.gitignore` | `build/`, `dist/`, `app.lock`を追加 | - |

### ビルド検証結果

本コンテナ（Linux）上で実際に`pyinstaller`を実行し、Linuxバイナリとしてビルド・実行検証を行った（Windows実機でのビルドは別途必要）。

```text
python build_release.py --version 1.0.0
→ dist/ClamAVFrontend_v1.0.0_linux64.zip (115.9 MB)
```

検証項目:
- クリーンな展開先からの初回起動で`config.json`/`app.db`/`logs/`/`quarantine/`が実行ファイルと同階層に生成されることを確認
- 0001→0003のマイグレーションがバンドルされたAlembicスクリプトから正しく適用されることを確認
- フォルダごと別ディレクトリへ移動した後も、既存の`config.json`/`app.db`を使って正常に再起動できることを確認（ポータブル構成の検証）
- README.md/README_JA.mdが配布物に同梱されることを確認

### 実装中に発見・修正した重大な不具合

1. **PyInstallerフリーズ環境でのアプリディレクトリ誤認識**: `_APP_DIR = Path(__file__).resolve().parent`は開発時（`python app.py`実行）には正しく動作するが、PyInstallerでフリーズした実行ファイルでは`__file__`が`_internal/`（`sys._MEIPASS`）配下のバンドル内パスを指してしまうため、`config.json`・`app.db`・`logs/`が実行ファイルの隣ではなく`_internal/`の中に生成されてしまう不具合を発見した。これはconfig.jsonスキーマ定義書2.1節の「実行ファイルと同一ディレクトリ」というポータブル構成の要件に反する重大な不具合であり、実際にビルドして動かしてみなければ気づけなかった典型例だった。`sys.frozen`判定により、フリーズ時は`sys.executable`の親ディレクトリを使うよう修正した。
2. **`logging.config`の隠れ依存漏れ**: `alembic/env.py`が実行時に`from logging.config import fileConfig`するが、PyInstallerの静的解析では検出されず、フリーズ実行時に`ModuleNotFoundError: No module named 'logging.config'`でDBマイグレーション自体が失敗した（結果としてE5002エラーになる）。`hiddenimports`へ`logging.config`を明示追加して解消した。
3. **`win32timezone`のクロスプラットフォームビルド対応**: Windows専用モジュールをLinux上でビルドしようとするとエラーになるため、`sys.platform == "win32"`時のみhiddenimportsに含めるよう条件分岐した。

これら3点はいずれも「specファイルを書いただけでは気づけず、実際にビルドして起動検証して初めて発覚した」不具合であり、パッケージング設計書のみに基づく机上検証では見逃していた可能性が高い。実装ステップ計画書のPhase9完了判定「クリーンな状態から初回起動〜初期化〜各画面利用まで一通り動作すること」を厳密に実施した効果と言える。

### ビルドサイズについて

非機能要件詳細4.2節の見積り「100〜200MB程度」に対し、実測は約116MB（Zip圧縮後）・展開後約201MB。PySide6のQt本体が大部分を占めており、想定範囲内。UPXは方針どおり不使用。

### 未実施（Phase10以降に持ち越し・Windows実機必須）

- Windows実機でのビルド（`.exe`化、UAC/uac_admin=Trueマニフェスト埋め込みの実地確認）
- コード署名（Authenticode）の要否判断（インストーラ/パッケージング設計書8章 未確定事項3、未着手）
- アイコン素材（`icons/*.png`, `icons/clamQt.ico`）の作成・同梱
- SmartScreen警告の実地確認

## 24. Phase 10 実装状況（結合テスト・手動確認・性能検証）

詳細は `../docs/docs_phase10_report.md` を参照。要点のみ以下に記載する。

### 実施内容

| 項目 | 結果 |
|---|---|
| 自動テスト・カバレッジ | 158 passed, 1 skipped、カバレッジ86%（目安80%達成） |
| 起動時間 | 通常起動0.683秒（目安3秒以内、達成） |
| リアルタイム監視 常駐メモリ | 108.3MB（目安100〜200MB、範囲内） |
| フルスキャン性能（5万件） | 13.36秒、メモリ増加15.2MB（モックclamdでのアプリ側オーバーヘッドのみ計測） |
| 手動確認チェックリスト | 自動化可能な項目はすべて確認済み。Windows固有項目（UAC/レジストリ自動起動/Toast通知）は実機確認待ち |

### 追加した検証スクリプト

- `scripts/measure_startup_time.py`
- `scripts/measure_realtime_memory.py`
- `scripts/measure_full_scan_performance.py`

### Phase10で新たに発見・修正した不具合

**`app.py`の`_run_scheduled_scan()`がScanWorkerを誤った引数名（`recursive=`、正しくは`include_subfolders=`）で呼び出しており、実際にスケジュールジョブが実行されると`TypeError`で失敗する状態だった。** Phase6でこのクロージャを実装して以来、`SchedulerService`の単体テストは`run_scan`コールバック自体をモック化していたため、クロージャの中身は一度も実行検証されていなかった。Phase10の性能検証スクリプト作成中に同種のミスを自分で踏んだことをきっかけに発覚した。再発防止のため`tests/unit/test_scheduled_scan_regression.py`を追加した。

この不具合は「各フェーズの単体テストが緑でも、フェーズをまたぐ結線部分（コールバック注入等）は別途結合検証が必要」という教訓の典型例であり、Phase10を独立フェーズとして設けたことの意義を裏付ける結果となった。

### 未実施（Windows実機が必要）

- UACプロンプト・常時管理者権限起動の実地確認
- レジストリRunキー自動起動時のUAC再表示確認
- PowerShell Toast通知の実表示確認
- 実clamd環境でのフルスキャン所要時間計測
- 実際のWindows `.exe`ビルド

## 25. 全体まとめ

実装ステップ計画書のPhase 0〜10がすべて完了した。マイルストーンM1〜M6（M6はWindows実機確認を除く）を本コンテナで実施可能な範囲において達成している。残る作業はリリース準備（Phase11: README最終化、未確定事項の棚卸し、バージョンタグ付け）とWindows実機での最終確認。

## 26. Windows実機テスト結果（Phase 11）

Windows 11 Pro（Python 3.14.8 / 3.15.0、PySide6 6.12.0、ClamAV 1.5.4 / 定義28157、Windows Defender有効）で実施。

### 確認結果

| 項目 | 結果 |
|---|---|
| 自動テスト | 3.14・3.15とも 163 passed（`--basetemp=.pytest_eicar`をDefender除外して実行。実clamdでのEICAR検知CLM-I01含む）、カバレッジ86% |
| 実clamd（auto接続） | PING/VERSION/SCAN/ディレクトリスキャン/INSTREAM（EICARをメモリから送信→FOUND）すべて正常 |
| 開発版起動（3.14 / 3.15） | 起動完了まで約2.4秒、常駐メモリ152MB / 157MB |
| `.exe`ビルド | 成功。Zip 60.8MB。`requireAdministrator`マニフェスト・バージョンリソース埋め込み確認、コード署名なし |
| `.exe`起動（UAC承認後） | 起動完了まで1.1秒、常駐メモリ135MB、config.json/app.db/logs/quarantineがexe同階層に生成（ポータブル構成OK） |
| 多重起動防止 | 後発プロセスは即時サイレント終了 |
| レジストリ自動起動 | 登録/確認/解除とも正常 |
| Toast通知 | AppID「ClamAVFrontend」のまま通知センターへの到達を履歴APIで確認 |

### 発見・修正した不具合

1. **Python 3.15で起動不能**: 3.15の`concurrent.futures`は`ThreadPoolExecutor`を遅延import（PEP 810）するが、PySide6を先にimportすると遅延importが解決されずプロキシのまま残り、APScheduler起動時に`TypeError: 'lazy_import' object is not callable`で落ちる。`app.py`と`tests/conftest.py`の先頭（PySide6より前）で実体化して回避した。
2. **隔離ファイルの完全削除がWindowsで必ず失敗（E3001）**: 隔離時に読み取り専用にしているため`unlink()`が`PermissionError`になっていた。復元処理と同様に書き込み可へ戻してから削除するよう修正した。
3. **通常権限での起動が例外で落ちる**: 管理者権限インスタンスが作成した`Global\`mutexに通常権限から`CreateMutex`するとアクセス拒否例外になり未処理だった。`ERROR_ACCESS_DENIED`を「起動済み」として扱うよう修正した。
4. **除外判定がWindowsのパス表記差を吸収しない**: 大文字小文字・区切り文字の違いで除外が効かなかった。`os.path.normcase(os.path.normpath())`で正規化して比較するよう修正した。
5. **開発時の自動起動登録が不正**: 非フリーズ時に`sys.executable`（python.exe）のみがRunキーに登録されていたため、app.pyのパスを含むコマンドを登録するよう修正した。
6. **アイコン素材未配置だとトレイに何も表示されず操作不能になる（重大）**: 空の`QIcon`だとWindowsではトレイアイコンが表示されないため、×ボタンでトレイ格納した後はメイン画面に戻ることも終了することもできなかった（再起動しても多重起動防止でサイレント終了する）。素材が無い場合はQt標準アイコンで代替するよう修正した。正式なアイコン素材は引き続き要作成。

### テスト側の修正

- 想定外のモーダルダイアログでテストが無限停止していたため、`conftest.py`にautouse fixtureを追加し、表示された場合は即時returnしてテスト失敗とするようにした
- fake_clamd_serverを使うテストはファイル内容に依存しないため、EICARの代わりにダミー内容（`FAKE_INFECTED_CONTENT`）を使うようにした。実EICARが必要なテストは`eicar_file` fixtureが実行時に生成し、常駐AVにブロックされた場合はスキップする（`tests/fixtures/eicar.txt`は不要になった）
- Linux前提のテストがWindowsで実レジストリ・実mutexに触れていたため、経路を明示・mutex名をテストごとに一意化した
- 通知テストの一部が`subprocess.run`をモック化しておらず、実際のPowerShell Toast（約1秒）を表示していたため待ち時間内にメール送信へ到達せず不安定だった。モック化して解消した

### 未確定事項への対応（Phase 11）

| 未確定事項 | 決定・対応 |
|---|---|
| 自動起動の設定 | 設定画面「一般」タブのチェックボックスを保存した時点で、OS側（Runキー / XDG Autostart）へ即時反映するようにした（従来は次回アプリ起動時まで反映されず、OFFにしても次のOS起動時には自動起動していた）。失敗時はE8002をダイアログ表示 |
| ジョブの重複実行の排他制御 | 入れる。同一ジョブIDが実行中なら2回目は実行しない。「今すぐ実行」ではE4004（新設、`JobAlreadyRunningError`）を表示、スケジュール発火ではログ記録のみでスキップ。別ジョブ同士は排他しない。エラーコード一覧・例外設計書4.4節にE4004を追記 |
| アイコン素材 | ClamAV公式ロゴは商標のため改変せず、オリジナル図案を`scripts/generate_icons.py`で生成（盾の中の二枚貝＝ClamAV、盾に弾かれて飛ぶ赤いウイルス＝防御、トレイ状態は右下バッジ）。`icons/tray_*.png`・`icons/clamQt.ico`（16〜256px）・`icons/app.png` |
| EICARテストファイル | リポジトリに置かず、`eicar_file` fixtureがテストごとに一時ディレクトリへ生成する |

### READMEの一本化（Phase 11）

`clamav_frontend/README.md`（英語）・`README_JA.md`（日本語）を廃止し、リポジトリ直下の `README.md`（日本語）/ `README_en.md`（英語）に一本化した。GitHubのリポジトリ説明と配布物同梱の説明書を兼ねる。`build_release.py` は直下の2ファイルを配布物へコピーし、見つからない場合はビルドを中断する（22章の記述は当時のもの）。

### Phase 11 追加対応で発見・修正した不具合

7. **exe版で同梱アイコンを参照できない**: specの`datas`で同梱したアイコンは`_internal/`（`sys._MEIPASS`）に展開されるが、`app.py`は`_APP_DIR`（exe同階層）の`icons/`を参照していた。読み取り専用リソースは`_RESOURCE_DIR`から参照するよう分離した。
8. **ウィンドウ・タスクバーのアイコン未設定**: `QApplication.setWindowIcon()`で`clamQt.ico`を設定するようにした。

### 未実施

- 修正後の`.exe`での目視確認（トレイアイコン・ウィンドウアイコン表示、トレイメニューからの終了、設定画面からの自動起動切替）
- コード署名の要否判断・SmartScreen警告の確認
- 「今すぐ実行」がGUIスレッド上で同期実行される（スキャン中は画面が固まる）。バックグラウンド実行化は別途検討
## 27. Linuxリアルタイム監視（clamonacc連携、Phase 11）

未確定事項「Linux側をwatchdog継続かclamonaccか」はclamonaccに決定した。fanotifyでファイルを開く前に検査でき、clamd.confの`OnAccessPrevention yes`で感染ファイルへのアクセス自体を遮断できるため（watchdog/inotifyは変更後の通知のみで遮断不可）。

### 実装したモジュール

| モジュール | 内容 |
|---|---|
| `core/clamonacc_manager.py` | ClamonaccRealtimeManager（RealtimeManagerと同一インターフェース: start/stop/is_running/add_target/remove_target、detected/watch_target_lost/scan_errorシグナル、mode_name） |
| `core/realtime_factory.py` | 方式選択。Windows=watchdog、Linux=clamonacc（clamonacc/pkexecが無ければwatchdogへフォールバック） |
| `core/config_model.py` | realtime.linux_backend / clamonacc_path / clamonacc_config_file / clamonacc_fdpass を追加 |
| `core/exceptions.py` | E2005 ClamonaccError を追加 |
| `gui/screens/realtime.py` | 方式ラベルに実際の方式を表示、clamonacc異常終了時にトグルを戻す |

### 設計上の決定事項

- **root権限**: clamonaccはfanotifyのためroot必須。GUIは一般ユーザーで動くため、`pkexec /bin/sh -c <監視スクリプト> clamonacc ...` でrootシェルを1つ起動する。認証は開始時の1回のみ
- **停止方法**: rootシェルはGUIが保持する標準入力パイプのEOFを待ち、EOFでclamonaccをkillする。停止時の再認証が不要で、GUIがクラッシュしてもパイプが閉じてclamonaccが自動停止する（rootプロセスの取り残し防止）
- **検知の取得**: clamonaccの`--log`ファイル（GUIが一般ユーザー所有で事前作成し、rootは追記のみ）を末尾から読み、`<パス>: <ウイルス名> FOUND`行を検知として扱う。検知時アクション（自動隔離／通知のみ）はwatchdog実装と共通
- **監視対象・除外**: DBから`--watch-list`/`--exclude-list`ファイルを生成。アプリ自身のlogs/quarantine/clamonaccフォルダは常に除外（自己検査防止）

### 既知の制限

- clamonaccの除外はフォルダ単位のみ。ファイル・拡張子除外は検知後のアクションを行わない形でアプリ側で反映（検査自体は行われる）。「プロセス」除外は対象外（watchdog実装と同じ）
- 監視対象の追加・削除はclamonaccの再起動で反映するため、再度認証を求められる
- `OnAccessPrevention yes`の場合、感染ファイルを開けないため自動隔離（SHA256計算時のopen）が失敗し得る。その場合もclamonaccがアクセスを遮断しているため被害は防がれる
- clamd側の設定（clamd.confのLocalSocket、`OnAccessExcludeUname clamav`等）は利用者が行う前提

### テスト結果

```text
Windows（Python 3.14 / 3.15）: pytest -q --basetemp=.pytest_eicar
  194 passed, 6 skipped（Linux/root専用テスト等）
  core/clamonacc_manager.py カバレッジ90%、core/realtime_factory.py 100%
Linux実機（WSL2 Ubuntu 26.04、kernel 6.18、ClamAV 1.5.4、Python 3.14、PySide6 6.12）:
  一般ユーザー: 194 passed, 6 skipped（実clamdでのEICAR検知CLM-I01を含む）
  root: tests/integration 7 passed, 1 skipped
```

`tests/integration/test_real_clamonacc.py`（Linux・root・clamonacc・clamd稼働時のみ実行）で、実clamonaccによる以下を確認した。

- EICARファイルへのアクセスで検知され、自動隔離まで到達する
- 除外フォルダ（--exclude-list）内のファイルは検知されない
- 停止後にclamonaccプロセスが残らない（rootシェルの標準入力EOFによる停止）
- 一般ユーザー・認証エージェント無しではpkexecが失敗し、E2005として通知される（`CLAMQT_TEST_PKEXEC_NO_AGENT=1`で実行）

WSLでの検証環境構築: `apt-get install clamav clamav-daemon clamdscan pkexec`、clamd.confへ`OnAccessExcludeUname clamav`を追記しclamav-daemonを再起動。

### Linux実機テストで発見・修正した不具合

9. **Linuxでユーザーファイルのスキャンがほぼ全てERRORになる（重大）**: SCANコマンドはclamdプロセス自身（clamavユーザー）がファイルを開くため、0700/0750のホームディレクトリ配下は`Permission denied. ERROR`になっていた（Ubuntuのホームは既定0750）。Permission denied/Access deniedの場合はアプリ自身が開いてINSTREAMで送り直すようにした（`ClamAVService.scan_file`）。送信上限超過時はファイル単位のERRORとして返す。
11. **ステータスバー・トレイアイコンの状態が更新されない**: ステータスバーの3項目（clamd稼働状態／定義DB更新日時／リアルタイム監視）はラベルを作るだけで一度も更新されず「clamd: 確認中...」のままだった。トレイアイコンの「エラー（clamd未起動）」状態も未実装で、「監視中」もトレイメニューから切り替えた場合しか反映されなかった。ホーム画面の`refresh_status()`が`AppStatus`を`status_refreshed`シグナルで発行し、app.pyでステータスバー・トレイアイコンへ反映するようにした。clamdの状態は60秒ごと（定義更新完了時も）、リアルタイム監視のON/OFFは2秒ごと（clamdへの問い合わせなし）に更新する。
10. **実clamd結合テストがLinuxで常にスキップされる**: `requires_real_clamd`がTCP 3310のみ確認しており、Linux既定のUNIXソケット接続を検出できなかった。UNIXソケットも確認するよう修正した。
## 28. 設計書と実装の突き合わせ（Phase 11）

設計書（docs/）と実装を両方向で突き合わせ、(1) 重大な不足 → (2) 中程度の不足 → (3) 設計書の更新 の順に対応した。

### (1) 重大な不足の修正

| 内容 | 対応 |
|---|---|
| スケジュールスキャンが検知結果を捨てていた | `core/scheduled_scan.py`を新設。検知時はリアルタイム監視の「検知時アクション」設定に従い隔離・通知し、最終実行結果に「検知あり n件」を記録（利用者確認済み）。clamd接続不可・対象パス無しはジョブ失敗 |
| 検知・スキャン・隔離・更新がログに残らない | `core/logger.py`に`log_event()`とカテゴリを追加し、ログ行に` <スキャン>`等を記録。ログ画面は明示カテゴリ→エラーコード帯→システムの順で分類 |
| リアルタイム画面で除外を登録できない | 除外の追加（区分ごとに参照/文字入力）・削除・有効/無効切替を追加。clamonaccはフォルダ除外変更時に再起動 |
| ジョブ編集ダイアログの既定Cron式が空（保存でE4002）、対象パス未入力で保存可 | 初期値の反映と入力チェック（ダイアログ内に赤字表示）を追加 |

### (2) 中程度の不足の修正

ホーム画面（アクティビティ・画面遷移・起動ボタン・停止確認）、トレイ（今すぐスキャンの実行と吹き出し、検知ありの解除）、スキャン画面（クイック/フルの対象確定、ファイル追加・削除、存在確認、エラー表示、無視、状態表示、200ms間隔の表示更新）、隔離画面（期間、省略表示、詳細、一括削除、復元時の同名ファイル）、スケジュール画面（人間可読表示、次回実行、最終実行結果、有効/無効、今すぐ実行のバックグラウンド化）、設定の即時反映とE8002時の戻し、ログのカテゴリフィルタ、リアルタイム画面（監視対象の有効/無効、初回案内、0件時の無効化、E2001表示、他画面との状態同期）、スキャンの読み取りタイムアウト撤廃、clamdのサービス起動（`core/clamd_process.py`）、リアルタイム用接続枠の確保、E2003/E2004のログ、多重起動時のログ、全エラーダイアログへの「詳細を見る」、表の列幅・高さ。

利用者確認で決定した仕様: フルスキャン=システムドライブのみ、クイックスキャン=デスクトップ・ダウンロード・ドキュメント・一時フォルダ（Windowsはスタートアップも）、clamd停止時=サービスとして起動（ホーム画面「起動」ボタン＋停止検知時に1回自動）。

### (3) 設計書の更新

- 矛盾の解消（利用者確認済み）: ×ボタンは常にトレイ格納（画面設計書の「タスクトレイに常駐」設定を削除）、`realtime.default_enabled`の既定はfalse、Linuxのロックファイルは設計書どおり`~/.local/share/clamav-frontend/app.lock`（実装を修正）、freshclamの失敗はE8004を新設（E8003から分離）、監視対象0件時は既定の監視対象を持たない
- 新規: `docs/Linuxリアルタイム監視詳細設計書.md`
- 各設計書の末尾に「Phase 11 改訂」の節を追加（画面設計書11章、clamdソケット通信プロトコル設計書9章、Windowsリアルタイム監視詳細設計書11章、タスクトレイ常駐アプリプロセス設計書10章、通知機能詳細仕様書9章、詳細設計書6章、基本設計書6章、インストーラ/パッケージング設計書9章、非機能要件詳細9章、テスト仕様書8章、DBマイグレーション設計書9.1節、エラーコード一覧6.1節）。config.jsonスキーマ定義書に`clamd.auto_start`/`clamd.service_name`等を追記

### 未対応として残したもの

- E2002（watchdogのバッファオーバーフロー）: watchdogがアプリへ通知する手段を提供しないため未実装
- ~~スキャンの「応答待ち」表示、clamd接続キュー待ちの「待機中」表示~~ → 29章で実装
- ~~Windows Toastクリック時の隔離画面への遷移~~ → 29章で実装

## 29. 残課題の実装（Phase 11）

| 項目 | 実装 |
|---|---|
| スキャンの「応答待ち」表示 | ScanWorkerの監視スレッドが、1ファイルのclamd応答待ちが10秒以上続くと`activity`シグナル（"stalled", パス, 経過秒）を出し、スキャン画面の状態欄に「応答待ち（n秒）: ファイル名」を表示。「停止」で応答待ちの通信を打ち切り直ちに中断（`ClamdClient.abort_thread()`、スレッドごとに通信中ソケットを管理）。打ち切りによる通信エラーは表示しない |
| 接続待ちの「待機中」表示 | オンデマンドスキャンが接続枠（上限-1）の空きを待つ間「待機中（リアルタイム監視のスキャンを優先しています）」を表示。待機中も「停止」で中断可。応答待ちの計測は通信開始時点から（`scan_file(on_wait=, cancelled=, on_start=)`） |
| Toastクリックで隔離画面 | `core/activation.py`。検知のToastに`clamqt:quarantine`（プロトコルアクティベーション）を付け、URLスキームをHKCUへ登録。クリックで補助プログラム（`notify_activate.py`、配布版は`ClamAVFrontendActivate.exe`。ウィンドウなし・UAC不要）が`%LOCALAPPDATA%\clamav-frontend\show_request`へ固定文字列を書き、起動中のアプリがQFileSystemWatcherで検知して隔離画面を表示（exeを直接起動するとUACが毎回出るため） |

### Windows実機テストで発見・修正した不具合

- **依頼ファイルが書き込み中で読めない**: フォルダの変更通知はPowerShellの書き込み完了前に届き、Windowsでは書き込み中のファイルを開くとPermission deniedになる。初回の実機確認では隔離画面へ切り替わらなかった。200ms間隔で最大10回読み直すよう修正し、通知（バナー・通知センター）のクリックで隔離画面へ切り替わることを確認した
- **クリック時にコンソールが一瞬表示される**: 当初はPowerShell（-WindowStyle Hidden）で依頼ファイルを書いていたが、起動の瞬間にコンソールが表示された。ウィンドウを持たない補助プログラム`notify_activate.py`（配布版は`ClamAVFrontendActivate.exe`、console=False・uac_admin=False、本体と同じフォルダで`_internal/`を共有）に置き換え、一時ファイル経由で書くようにした。配布版exe（管理者権限）で、クリック時にウィンドウ・UACが表示されず隔離画面へ切り替わることを確認した。Zipは37.1MB→39.0MB

### テスト結果

```text
Windows（Python 3.14 / 3.15）: 275 passed, 6 skipped、カバレッジ88%
Linux（WSL2 Ubuntu）: 一般ユーザー 275 passed（2回連続）、root結合テスト 7 passed
```

## 30. Linux実機で発見・修正した不具合（Phase 11）

Arch Linux（一般ユーザーで`python app.py`）で発見した。

| 不具合 | 原因 | 修正 |
|---|---|---|
| 定義更新が終了コード2で失敗し、原因が分からない | freshclamは`/var/lib/clamav`・`/var/log/clamav`（所有者clamav）へ書き込むため、一般ユーザーではPermission denied（Initialization error）。失敗の詳細に終了コードしか出していなかった | 手動更新（定義更新画面・ホーム・トレイ）は`pkexec freshclam`で実行。自動・スケジュール更新は無人で認証できないため実行せず、`clamav-freshclam`サービスが動いていればスキップ（最終実行結果「スキップ（OSのサービスが更新）」、履歴に記録しない）、動いていなければサービスの有効化を案内して失敗。失敗の詳細にfreshclamのERROR/WARNING行（最大3行）を含め、ダイアログ・通知・ログ（E8004）に表示 |
| `clamav-freshclam`サービス稼働中に手動更新すると終了コード2で失敗する（Arch Linux実機） | サービスの`freshclam -d`が`/var/log/clamav/freshclam.log`をflockで保持しており、手動の`pkexec freshclam`が「Failed to lock the log file ... Resource temporarily unavailable」で初期化に失敗した（freshclamのロックはログファイルのみで、DBディレクトリはロックしない） | Linuxでサービスが稼働中のときは手動実行（rootの場合は自動・スケジュールも）に`--log=/dev/null`を付ける。出力はこれまでどおりアプリのログ・定義更新画面に残る |
| ログに`[ [E8004]]`と二重のタグが出る | ログのフィルタが`record.error_code`自体を` [E8004]`に書き換えており、別のハンドラで再度整形されていた | 表示用の属性`error_code_tag`を別に設け、`error_code`は書き換えない |
| コンソールに同じログが2行ずつ出る | Alembicの`fileConfig`がルートロガーにコンソールハンドラを付け、アプリのロガーから伝播していた | アプリのロガーを`propagate=False`にし、`setup_logger`の再呼び出し時は古いハンドラを閉じて外す |
| freshclamが見つからないと定義更新画面が実行中のまま止まる | `update_now`が例外を送出し、ワーカーが完了を通知できなかった | 例外を送出せず失敗（E8004）を返す |
| `sudo python app.py`で起動できない | rootのPythonにはPySide6が入っていない（venvは一般ユーザーのもの） | 一般ユーザーで起動する手順をREADMEに追記（root権限が必要な処理はpkexecで認証） |
| リアルタイム監視が「監視中」なのに検知しない | clamonaccは監視の設定（除外・inotify/fanotifyの登録）に失敗すると、DDDのスレッドだけ終了してプロセスは監視なしで動き続ける。アプリは終了マーカーでしか異常を検出しておらず、`ClamInotif: can't exclude`の行をWARNINGログに残すだけだった | 監視設定の失敗を示す行（Linuxリアルタイム監視詳細設計書11.2）を検出したらclamonaccを停止し、E2005（詳細に該当行と対処方法）を通知する。停止に伴う終了マーカーは二重に通知しない |
| Arch Linuxでclamonaccが除外フォルダを除外できない（`can't exclude`）・サブフォルダを監視しない | glibc 2.44の`fts_children()`がすべての子を`FTS_NSOK`で返し、clamonaccのDDDが`FTS_D`の子しか記録しないため、どのディレクトリにも子が無い扱いになる（clamonaccの`hash.c`を取り出して実機で再現。ClamAV Issue #1797） | clamonacc側の不具合のため未修正。上記の検出でE2005とし、詳細でwatchdog監視（`realtime.linux_backend: "watchdog"`）への切り替えを案内する。READMEに記載 |
| clamonaccが「OnAccessExcludeUname等が必要」で起動しない | ローカルのclamdではclamd.confの`OnAccessExcludeUname`等が必須（設計書・READMEは「推奨」と誤記） | 設計書・READMEを「必須」に訂正。起動前（認証前）にclamd.confを確認してE2005で案内（追加すべき行のユーザー名はclamd.confの`User`） |
| clamdの一時フォルダ（`/tmp`）を監視対象にするとclamonaccが無限ループする（ソースで確認） | clamonaccの`--watch-list`の処理が、`TemporaryDirectory`と同じパスで次の行へ進まない | 監視対象からclamdの一時フォルダと`/`を外して起動し、E2006を新設して通知 |
| 1つのファイルで検知の通知が2回出る（ソースから推定） | 隔離時のSHA256計算でアプリがファイルを開くと、clamonaccがそれも検査して同じFOUND行を再度出力する | 同じパスの検知が10秒以内に続いた場合は重複として通知・隔離しない |
| clamdがTCP接続の場合に`--fdpass`が働かない | clamd.confに`LocalSocket`が無いと`--fdpass`は黙って無視され、clamdがパスを開く方式になる（clamdのユーザーが読めないホームフォルダを検査できない） | `LocalSocket`が無い場合と`clamonacc_fdpass: false`の場合は`--stream`を使う。clamd.confの読み取りは`core/clamd_conf.py`（`OnAccessMountPath`はWARNINGログ、`OnAccessDisableDDD`はE2005） |
| リアルタイム監視（watchdog）で、ブラウザでダウンロードしたファイルを検査しない | 設計書4.1節で`on_moved`（リネーム・移動）を対象外としていた。ダウンロードは`.part`等に書いてから最終的な名前に変えるため、完成したファイルは移動イベントでしか通知されない（Linux実機で、名前変更後のEICARが検知されないことを確認。Windowsも同じ処理） | `on_moved`で移動先のファイルを検査する（除外判定も移動先で行う）。フォルダの移動はwatchdogが中のファイルごとの移動イベントを発行するため個別に扱わない。設計書4.1節・11章を改訂 |
| ブラウザの一時ファイル（`*.tmp`・`*-journal`等）で「Permission denied」のE2004が大量に記録される | 一時ファイルはスキャンの前に削除・名前変更される。clamdは読めないフォルダでは存在しなくてもPermission deniedを返し、アプリのINSTREAMでの読み直しもファイルが無いため元のエラーをそのまま返していた | リアルタイム監視では、スキャン時にファイルが無ければリトライ・E2004を行わない（DEBUGログ）。ClamAVServiceはファイルが無い場合「ファイルが見つかりません（スキャンの前に削除または移動されました）」を返す |
| Linuxのスキャンが遅く、clamd.logに`File path check failure`の警告がファイル数分記録される | clamd（clamavユーザー）は0700のホーム配下を読めず、ファイルごとに`SCAN`（権限エラー）→`INSTREAM`と2回通信していた。`INSTREAM`は100MBを超えるファイルを検査できなかった | UNIXソケット接続ではアプリが開いたファイルを`FILDES`（ファイルディスクリプタ渡し）でclamdへ渡す（`ClamdClient.scan_fd()`）。使えない場合は従来どおり`SCAN`→`INSTREAM`。プロトコル設計書9.6節。実機で1MB超の40ファイルが1.00秒→0.27秒 |
| 隔離の記録が増え続け、隔離画面で削除しても消えない（隔離の無限ループ） | 移動イベント対応（上の行）で、隔離フォルダへの移動も検査されるようになった。watchdog方式ではアプリ自身のフォルダを除外しておらず（clamonacc方式のみ除外）、監視対象（ホーム）の中にある隔離フォルダのファイルが再び検知されて隔離が繰り返された（約2時間で7,500回） | watchdog方式でもアプリ自身のフォルダ（logs・quarantine・clamonacc）を常に検査しない（`RealtimeManager(extra_exclude_dirs=)`、`exclude_service.is_under_any_folder()`）。二重の防止策として`QuarantineService.quarantine()`は隔離フォルダ内のファイルを拒否し、clamonacc方式も検知後のアクションを行わない。テストで防止策を外すと2秒で141回繰り返されることを確認 |

WSL2 Ubuntuの実機（実際のfreshclam）で確認した結果: 一般ユーザーのスケジュール実行→サービス案内付きの失敗、サービス稼働中→スキップ、認証エージェントなしの手動実行→「管理者認証がキャンセルされたか、失敗しました」、root→「最新」。

Arch Linuxの実機（glibc 2.44、ClamAV 1.5.4、実際のclamonacc・pkexec）で確認した結果:

- サブフォルダの無い監視フォルダでEICARを作成→検知・隔離。clamonaccは1ファイルに対してFOUND行を5回出力したが、通知・隔離は1回（重複の抑止が有効）
- ホームフォルダ（`/home/<利用者名>`）を監視（アプリのlogs等を除外）→ 約10秒後に`can't exclude`を検出してclamonaccを停止し、対処方法付きのE2005。clamonaccのプロセスは残らない
- 認証なしで判定する項目: OnAccessExcludeUnameの無いclamd.conf→E2005（clamonaccを起動しない）、OnAccessDisableDDD→E2005、`/tmp`のみ監視→E2006、LocalSocketの無いclamd.conf→`--stream`、実機のclamd.conf→`--fdpass`

### テスト結果

```text
Windows（Python 3.14 / 3.15）: 287 passed, 6 skipped
Linux（WSL2 Ubuntu）: 一般ユーザー 287 passed（2回連続）、root結合テスト 7 passed
```

clamonaccの見直し後（Arch Linux、Python 3.14、一般ユーザー）: 312 passed, 6 skipped。root結合テスト（`tests/integration/test_real_clamonacc.py`）は未実施（sudo不可の環境のため。代わりに上記の実機確認を実施）
移動イベント対応後（同上）: 318 passed, 6 skipped。実機（watchdog・実際のclamd）で、`.part`から名前を変えたEICAR、フォルダごと名前を変えた中のEICARが検知・隔離され、`.part`のE2004が記録されないことを確認
FILDES対応後（同上）: 324 passed, 6 skipped（実clamdの結合テスト`test_real_clamd.py`を含む。FILDESで検知）
隔離の無限ループ修正後（同上）: 331 passed, 6 skipped。実機（watchdog・実際のclamd）で、監視対象の中にアプリのフォルダがある配置でEICARを隔離し、隔離が1回だけであることを確認

## 31. ログのクリア（Phase 11）

ログ画面に「ログをクリア」ボタンを追加した（利用者の要望。隔離の無限ループでapp.logが約12MBになったことが契機）。

| ファイル | 内容 |
|---|---|
| `core/logger.py` | `clear_logs(log_dir)`: `app.log`は出力先のロックを取って中身を空にし（ファイルを開いたまま。Windowsでは開いているファイルを削除できないため）、`app.log.*`（日付付きの過去のファイル）を削除する。最後に「ログをクリアしました（過去のログファイル n件を削除）」を記録 |
| `core/log_reader_service.py` | `LogReaderService.clear()` |
| `gui/screens/log.py` | 「CSV出力」の右に「ログをクリア」。確認ダイアログの後に消去して再読込 |

- 範囲は「すべて」のみ（利用者確認済み）。古いログは保持期間（`logging.retention_days`）で自動的に消えるため、期間指定のクリアは設けない
- clamonaccのログは、監視中に読み取り位置（オフセット）を使っているため対象外
- 実機確認（Arch Linux）: 実際のログ（5万3千行、11.8MB）のコピーを画面から消去し、0.16秒で「ログをクリアしました」の1行になることを確認。テスト: 336 passed, 6 skipped

## 32. セキュリティレビューでの修正（Phase 11）

ソース精読・pip-audit・banditによるレビューで見つかった問題のうち、Linux/共通分を修正した。

| 問題 | 深刻度 | 修正 |
|---|---|---|
| SMTPのTLS証明書を検証していない（`SMTP_SSL`・`starttls`にSSLコンテキスト未指定で、既定のCERT_NONEになる。中間者攻撃で資格情報が漏れ得る） | 中 | `ssl.create_default_context()`を`SMTP_SSL(context=)`・`starttls(context=)`へ渡す（`core/notification_service.py`）。自己署名・CN不一致のサーバを拒否することをテストで確認 |
| Toast通知へのPowerShellコード注入（高）。検知ファイル名のUnicode引用符（U+2019・U+2018）で`'`→`''`エスケープを回避し、PowerShellに任意コードを注入できた。ClamQtは管理者権限で動くため管理者権限で実行される（Windows実機で再現確認） | 高 | 検知内容（title/body）をスクリプトに埋め込まず、環境変数`CLAMQT_TOAST_TITLE`/`CLAMQT_TOAST_BODY`で渡し`$env:`で読む（`core/notification_service.py` `_send_windows_toast`）。環境変数の値はコードとして解釈されない。悪意あるファイル名がスクリプトに現れないことをテストで確認。実Toast表示はWindowsで再検証 |

| app.dbが誰でも読める権限（0644）（低）。隔離した元ファイルのパス・ウイルス名・監視対象等が入り、マルチユーザー環境で他ユーザーに読まれ得る | 低 | 接続時にapp.dbと付随ファイル（-wal/-shm/-journal）を0600にする（POSIXのみ。`db/database.py` `_restrict_db_permissions`）。存在しなければ先に0600で作成し、既存の緩い権限のDBも接続時に直す。Windowsは対象外 |

| logs/とログファイルが誰でも読める権限（0755/0644）（低）。ログに検知パス・ウイルス名・監視対象等が平文で入り、マルチユーザー環境で他ユーザーに読まれ得る（Windows側の検証で指摘） | 低 | `logs/`を0700、ログファイルを0600にする（`core/logger.py`。ファイルを開くたびに0600にするハンドラ`_OwnerOnlyTimedRotatingFileHandler`で、ローテーション後の新ログ・退避ログも0600。既存の緩い権限のログも起動時に直す）。POSIXのみ |

別検証（Windows）: 上記Toastの修正の実機再検証（通知が正しく表示される・注入が再現しない）。


## 33. 画面レイアウトの刷新（10画面化・製品名ClamQt）

`docs/screenshots_i18n.zip`のサンプルに合わせて画面レイアウトを変更した。画面設計書（8画面・左ナビの文字だけのリスト）と異なる。

| 項目 | 内容 |
|---|---|
| 共通（`gui/theme.py`・`gui/icons.py`） | 暗色テーマ（Fusion＋パレット＋QSS。OSのライト/ダークに関わらず暗色）。強調色は赤。ボタンは`set_variant(button, "primary"/"danger")`。ナビ・カードのアイコンはQPainterで描く（画像・アイコンフォントは同梱しない） |
| `gui/main_window.py` | 上部ヘッダー（≡で左ナビをアイコン幅に折りたたみ、アプリのアイコン、製品名「ClamQt」＋副題「ClamAV フロントエンド」、右上に稼働状態）。左ナビはアイコン付き10項目、下部に「Powered by ClamAV」と定義DBの版。ステータスバー左に「ClamQt 1.0.0」。タイトルバーは「ClamQt」 |
| `gui/screens/base.py` | 画面タイトル＋説明（`screen_subtitle`）、内容をカード（`begin_card()`〜`end_card()`）にまとめる。画面全体を縦スクロール、内容の最大幅1100px。ナビの表示名（`NAV_ITEMS`）と画面タイトル（`screen_title`）は別にした |
| 左ナビの順序・表示名 | ホーム／ウイルスと脅威の防止（scan）／セキュリティ インテリジェンス（update）／保護の履歴（log）／隔離されたアイテム／ClamAV エンジン サービス（新規）／リアルタイム保護／スキャン スケジューラー／システム診断（新規）／設定。サンプルの表示名に合わせた |
| ホーム | 「保護の概要」: 状態のヒーローカード（エンジン・定義DB・リアルタイム監視）＋操作カード4枚（スキャン／定義／リアルタイム監視／エンジン サービス）＋直近のアクティビティ。clamdの「起動」ボタンはエンジン サービス画面へ移した。`AppStatus.engine_version`を追加 |
| ClamAV エンジン サービス（`gui/screens/engine.py`） | 状態（接続先・エンジン版・定義DB・OSのサービス名）、開始／停止／定義データベースを再読み込み（RELOAD）／状態を確認（PING）、操作の出力。開始は従来どおりapp.pyの`ClamdStarter`、停止は`ClamdServiceController.stop()`（`sc stop` / `pkexec systemctl stop`、失敗はE8003）。停止・再読込・PINGは`BackgroundTask`（`core/background_task.py`）でバックグラウンド実行 |
| システム診断（`core/diagnostics_service.py`・`gui/screens/diagnostics.py`） | clamscan/freshclam/clamd/clamdscan/clamonaccの場所（PATH、Windowsは`Program Files\ClamAV`・Chocolateyのbinも探す）、clamd.conf・freshclam.conf・定義DBフォルダ・定義ファイル、clamdの接続先・OSのサービスの状態・PING・版、リアルタイム監視の方式を「ClamQt 診断レポート」にまとめ、ヘルスチェック結果（OK/注意/NG）を付ける。「レポートをコピー（パス伏せ字）」はホームフォルダ→`~`、ユーザー名・コンピューター名→`<ユーザー名>`/`<コンピューター名>`に置き換える（2文字以下の名前は置き換えない） |
| 既存8画面 | タイトル・説明・カード区切り・主操作ボタンの色のみ変更。ウィジェットと機能は変えていない |
| 製品名 | `core/version.py`（`APP_NAME`="ClamQt"）。タイトルバー・ヘッダー・ステータスバー・トレイのツールチップと吹き出し・診断レポートで使う。ClamAVのロゴは使わず独自アイコン（`icons/clamQt.ico`）を使う |

決定事項・注意点:
- エンジン サービス画面から停止したclamdは、停止検知時の自動起動（`config.clamd.auto_start`）で起動し直さない（app.pyの`clamd_start["user_stopped"]`。解除は「サービスを開始」で手動起動したときだけ。`auto_attempted`は稼働を確認すると解除されるため抑止には使わない）
- 抑止は停止を要求した時点で始める（`EngineScreen.bind_services`の`on_stop_requested`、app.pyの`_on_clamd_stop_requested`）。停止に失敗した場合（または別の操作の実行中で停止を始められなかった場合）は`on_stop_failed`で要求前の状態に戻す。当初は停止の成功後に抑止していたため、停止処理中（Linuxの`systemctl stop`がclamdの終了を待つ間、Windowsの`STOP_PENDING`中）に60秒ごとの定期確認のPINGが失敗すると自動起動が走り、pkexecの認証画面が再表示されるか停止直後にclamdが起動し直していた（コードレビューで指摘）
- Debian/Ubuntuで`clamav-daemon.socket`が有効だと、停止後もソケットへの接続でsystemdがclamdを起動し直すことがある（OS側の設定のためアプリでは制御しない）
- `sc stop`の1062（ERROR_SERVICE_NOT_ACTIVE）は成功扱い。起動の1056判定はWindowsのときだけに限定した
- 数値・日付の入力欄（QSpinBox / QDoubleSpinBox / QDateEdit / QTimeEdit）の上下ボタンが暗い背景に埋もれて見えにくかった（利用者の確認で指摘）。入力欄の背景・枠線をスタイルシートで指定したため、上下ボタンが小さな暗い四角になり、矢印も描かれていなかった。上下ボタンの背景・区切り線・ホバー/押下の色を指定し、矢印はQPainterで描いた三角形の画像（`gui/theme.py`の`_spin_arrow_rules()`）を`image: url(...)`で指定する。上下ボタンをスタイルシートで指定するとQtは矢印を描かず、枠線で三角形を作る指定も四角形になるため画像にした。画像はアイコンと同じく同梱せず、起動時にユーザー単位のキャッシュのフォルダ（`gui/theme.py`の`arrow_image_dir()`）へ上書きで書く（一時フォルダに書いていたときは消えずに溜まった。§88）。無効時は灰色の矢印。プルダウン（QComboBox）の矢印は見えているため変えていない

未対応（要判断）:
- メールの件名・本文（`core/notification_service.py`）、exeのバージョン情報（`version_info.txt`）、Linuxの自動起動`.desktop`の名前、配布物名`ClamAVFrontend_v*.zip`は「ClamAVフロントエンド / ClamAV Frontend」のまま
- 画面設計書（8画面）への反映、英語表示（サンプルのen_*）

テスト: 本開発機ではセキュリティソフトの影響があるため未実行（別PCで実行する）。起動確認のみ実施（起動シーケンス完了・例外なし、10画面のナビとヘッダーの表示を確認）

### コードレビューでの修正

| 問題 | 修正 |
|---|---|
| 処理の完了後もボタンが無効のまま戻らない（finishedをワーカースレッド内で発行するため、受け取った画面側で`is_alive()`がまだTrueに見える） | `core/background_task.py`の`BackgroundTask`を新設。実行中フラグをロック付きで持ち、finishedの前に下ろす。`ClamdStarter`もこれで実装し直した（重複していたスレッド処理を統一） |
| 利用者が停止したclamdが自動で起動し直される（`sc stop`は停止処理中で成功を返し、直後のPINGが通ると`auto_attempted`が解除される） | app.pyに`user_stopped`を追加。手動で起動したときだけ解除する。なお、OS側から起動されたclamdが後で落ちた場合も、手動で起動するまで自動起動しない |
| 自動起動の試行中に「サービスを開始」を押すと「起動中...」のまま固まり、自動起動の失敗がダイアログで出る | `_start_clamd`が起動を受け付けたかを返し、試行中なら受け付けない（`manual`も上書きしない）。画面は「既に実行中」と表示 |
| 配布版でsc実行時にコンソールが一瞬表示される | Windowsでは`creationflags=CREATE_NO_WINDOW`を付ける（`clamd_process.no_window_kwargs`。起動・停止・診断の`sc query`） |
| 画面・診断レポートの版とZip名の版がずれ得る | `build_release.py --version`の既定値を`APP_VERSION`にし、異なる値の指定はビルド中止 |
| 診断で存在しない指定clamd.confを「見つかった」と表示 | 指定先が無ければ注意として記録し、既定の場所を探す |
| 設定を保存しても診断が起動時の設定のまま | `DiagnosticsService`に`ConfigService`を渡し、診断のたびに`current`を読む |
| `_wrap()`の重複、home.pyの色の別名、サイドバーの折りたたみ状態を実際の幅で判定 | `base.wrap_layout()`に統一、別名を削除、状態をboolで持つ |

テスト: 未実行（別PCで実行する）。起動確認のみ（起動シーケンス完了・例外なし、自動起動の失敗処理がBackgroundTask経由で動くこと、ホーム画面の表示を確認）

### Windows実機の検証で発見・修正した不具合

| 問題 | 修正 |
|---|---|
| Python 3.15（UTF-8モードが既定）で、日本語Windowsの`sc`の出力（cp932）を`text=True`だけでは読めず、出力が空になる。1056（既に起動中）・1062（既に停止中）を判別できずE8003になり、エラーの詳細も空になる（起動側はPhase 11からの不具合） | `clamd_process.console_text_kwargs()`を新設し、Windowsでは`encoding="oem", errors="replace"`で読む（起動・停止・診断の`sc query`）。cp932のメッセージを実際の子プロセスに出させるテストを追加 |

テスト（Windows、Python 3.15.0 / 3.14.8）: 全テスト成功。実機のclamd（Chocolatey版1.5.4）で診断レポートの作成を確認

## 34. コードレビューでの修正（システム診断・freshclamの場所・伏せ字）

§33のシステム診断について、コードレビューの指摘を受けて次のように変えた（利用者と方針を確認済み）。

| 問題 | 修正 |
|---|---|
| 診断はfreshclamを既定のインストール先（`Program Files\ClamAV`等）で見つければOKにするが、定義更新（`UpdateService`）はPATHのfreshclamしか起動しないため、診断はOKなのに更新はE8004で失敗する | 実行ファイルは、アプリが実際に起動するのと同じ探し方で探す（PATH。freshclamは`update.freshclam_path`、clamonaccは`realtime.clamonacc_path`の指定）。見つからなければ注意。既定のインストール先（Windows）・sbin（Linux）は、見つからないときの案内（「○○にあります。PATHに追加するか、場所を指定してください」）にだけ使う |
| Linuxで一般ユーザーのPATHに`/usr/sbin`が無いと、正しく導入されたclamdを「NG: 見つかりません」と出す | clamdはOSのサービスとして起動しソケットで接続するため、実行ファイルの場所は表示のみにし、判定はPING・サービスの状態で行う。clamscan / clamdscanも本アプリでは使わないため表示のみ（以前は「動作には不要」の注意を出していた） |
| freshclamがPATHに無い環境で、場所を指定する方法が無い | `config.update.freshclam_path`（既定"freshclam"）を追加し、設定画面「定義更新」タブに入力欄と「参照...」を置いた。空欄・不正値は"freshclam"に戻す。保存すると`UpdateService.freshclam_path`に反映し、次の更新から使う |
| 伏せ字版のレポートに、clamdの接続先（社内のホスト名等）・ソケット・設定ファイルのパス・ホーム外のアプリのフォルダが残る | `DiagnosticsReport.sensitive`（伏せる値→置き換え後の文字列）を作り、`mask_report(extra=...)`で置き換える。対象: ソケット（設定と`LocalSocket`）、clamd.conf / freshclam.confのパス、clamonaccのパス、アプリのフォルダ、clamdのホスト名とグローバルIP（`is_sensitive_host`）。ループバック・プライベートIPは環境を特定できないため伏せない。ホスト名はグローバルかどうかを名前から判断できないため伏せる。追加の値はホームフォルダより先に置き換える（ホーム配下のアプリのフォルダを丸ごと伏せるため） |
| 診断のサービス名の既定値（clamd / clamav-daemon）を`ClamdServiceController`と別に持っている | `ClamdServiceController(config.clamd).service_name`を使う。設定の保存時に`clamd_controller.config`を差し替え、エンジン サービス画面のサービス名の表示も更新する（`EngineScreen.reload_service_name()`） |
| `find_clamd_conf()`の直後の`load_clamd_conf()`が候補をもう一度探す | 見つけたパスをそのまま読む |
| clamd.confの待ち受け先とアプリの接続先が違っても気づけない（接続できない原因として多い） | 診断で比べて、違えば注意を出す。TCP: `TCPSocket`の有無とポート、`TCPAddr`（無い・`0.0.0.0`・`::`はすべてのアドレス、localhost / 127.0.0.1 / ::1は同じものとみなす）。UNIXソケット: `LocalSocket`の有無とパス（`/var/run`→`/run`のリンクを考えて実パスでも比べる） |

決定事項・注意点:
- 実行ファイルが見つからない場合は、freshclamも含めて「注意」とする（NGにしない。利用者の判断）
- ClamAVのインストール内容（必要なコンポーネント、サービスが必須かどうか、Windows / Linuxの準備、接続先の既定値と変え方）をREADME（日英）の「動作要件」に書いた。Windowsの手順は実機で未確認（READMEにコメントで「要確認」を残した）
- 画面設計書11.2節の「ホーム画面の起動ボタン」に、エンジン サービス画面へ移したことを追記した

テスト: 未実行（別PCで実行する）。起動確認のみ。

### Windows実機の検証で発見・修正した不具合

| 問題 | 修正 |
|---|---|
| ChocolateyでClamAVを入れた環境で、clamdが定義を読み込んで正常に動作しているのに「NG: 定義ファイルが見つかりません」、clamd.confも見つからないと出る。PATH上の`clamd.exe`は本体を呼び出すだけの中継ファイル（shim）で、その隣（`chocolatey\bin`）にclamd.confや定義DBが無いため。サービスは`--config-file`で別の場所（例: `C:\ProgramData\clamav-webui\clamd.conf`）の設定を読んでいた | Windowsでは`sc qc <サービス名>`の`BINARY_PATH_NAME`から、サービスが実際に起動するclamd.exeと`--config-file`を取り出し（`_windows_service_command`）、clamd.confはその設定ファイルを最優先、freshclam.conf・定義DBフォルダはその設定ファイル・本体のフォルダを先に探す。レポートに「clamd（サービス）」の行を追加。さらに、定義ファイルの場所を特定できなくてもclamdが応答していれば（定義を読み込めている）NGではなく注意にする |

テスト（Windows、Python 3.15.0 / 3.14.8）: 全テスト成功。実機（Chocolatey版ClamAV 1.5.4、設定は`C:\ProgramData\clamav-webui`）で、clamd.conf・freshclam.conf・定義ファイル3つを検出し、ヘルスチェックがすべてOKになることを確認

## 35. 設定画面「ClamAV エンジン」タブ（コードレビュー後に追加）

clamdの接続先・サービス名・自動起動、clamonaccの場所をconfig.jsonの直接編集なしで変えられるよう、設定画面に「ClamAV エンジン」タブ（画面設計書8-7）を追加した（利用者と方針を確認済み）。

| 項目 | 内容 |
|---|---|
| 項目 | 接続方式（自動／TCP／UNIX ソケット。UNIX ソケットはLinuxのみ）、ホスト、ポート、UNIX ソケット（Linuxのみ表示）、OSのサービス名、停止を検知したら自動で起動、clamonaccの場所（Linuxのみ表示）。freshclamの場所は「定義更新」タブ（§34） |
| 反映のタイミング | 接続の設定はアプリの再起動後。サービス名・自動起動は保存直後。clamonaccの場所は次にリアルタイム監視を開始したとき（起動時にwatchdogへフォールバックしていれば再起動が必要） |
| 接続テスト | 入力中（保存前）の値で`core.clamd_client.check_connection()`（一時的な`ClamdClient`でPING→VERSION）を`BackgroundTask`で実行し、結果をボタンの右に表示する。画面はサービスを作らない方針のため、app.pyから`connection_tester`として渡す |
| 空欄 | ホスト・UNIX ソケット・clamonaccの場所は既定値に戻す。サービス名の空欄はOSの既定 |

決定事項・注意点:
- 設定画面は`ConfigService.current`をその場で書き換えるため、以前は`ClamAVService`（`ClamdClient`）が同じ`ClamdConfig`を共有しており、保存した接続先が通信の途中から部分的に使われ得た（FILDESを使うかは起動時に決まる等）。app.pyで`copy.deepcopy(config.clamd)`を渡し、接続の設定は再起動後に反映する形に揃えた。`ClamdServiceController`と停止検知時の自動起動（`config.clamd.auto_start`）は従来どおり現在の設定を使う
- 診断の「clamd.confの待ち受け先とアプリの接続先」の比較は保存済みの設定（`config_service.current`）を使う。接続の設定を保存してから再起動するまでの間は、実際の接続先（診断の「接続先」欄）と比較対象が異なる
- 診断の案内先（clamonaccの場所、接続の設定）を設定画面の「ClamAV エンジン」タブにした

テスト: 未実行（別PCで実行する）。起動確認のみ。

## 36. 全体レビューの推奨項目の修正

2026-10-23の全体レビュー（87件。利用者に報告ページで共有）のうち、普通に使っていて起きる「推奨」の項目を直した。IDは報告ページの指摘ID。

| ID | 問題 | 修正 |
|---|---|---|
| SU-1 | スケジュールのプリセット「毎週日曜 3:00」（`0 3 * * 0`）が毎週月曜に実行される。APSchedulerの`from_crontab`は曜日の0を月曜として扱う（3.11.3で確認）。画面の表示は日曜。7は不正な値になっていた | `core/scheduler_service.py`の`cron_trigger()`で、曜日欄を標準のcronの数え方（0と7が日曜）から曜日名（sun〜sat）に変換してから渡す。範囲（1-5、5-7）・列挙・間隔（*/2）・名前に対応。ジョブ編集ダイアログの検証も同じ関数を使う。DBに保存済みのcron式は変えない（もともと標準の数え方で、表示とも一致する） |
| SU-2 | 自動更新の初回が「登録（起動・設定保存）から実行間隔の後」なので、起動から4時間以内にPCを終了する使い方では一度も実行されない。関係のない設定を保存するたびに初回までの時間がやり直しになる | `set_auto_update(..., last_run=)`。前回の定義更新（`UpdateService.last_executed_at()`。手動・自動・スケジュール、失敗を含む）から間隔の後を初回とし、過ぎていれば（履歴が無い場合も）約2分後に実行する。同じ間隔での再登録では次回時刻を変えない |
| RT-3 | 自動隔離に失敗しても、通知・メールに「自動的に隔離されました」と出る。隔離したかを設定値（`default_action`）と最後の1件で決め、`detected`は隔離の前に出ていた | 両方の監視方式に`detection_handled(path, virus_name, quarantined)`を追加し、隔離を試した後の実際の結果で通知する（app.pyの結線を変更）。`DetectionEvent.quarantined`を持たせ、メールの「対応状況」は集約した検知の実際の結果から作る（一部だけ隔離できた場合の文面を追加。通知機能詳細仕様書9章） |
| RT-2 | clamdが止まっている間、ファイルのイベントごとに`scan_error`が出て、エラーダイアログが入れ子で積み重なり操作できなくなる | `RealtimeManager._scan_one`は、接続できなくなった最初の1回だけ`scan_error`を出してログに記録し、次に検査できた時点で解除する（再開もログに記録）。リアルタイム画面は、エラーダイアログの表示中に届いたエラーを重ねて表示しない |
| GU-2 | 設定画面が起動時の入力値のまま保存し、リアルタイム画面で「通知のみ」にした検知時アクションを「自動隔離」に戻す。×ボタンでの案内の表示済みも戻る | app.pyの画面切り替えで、設定画面を開くたびに`reload_from_config()`を呼ぶ。注意: トレイから画面を開き直したときも読み直すため、保存していない入力は破棄される |
| DB-7 | ログ画面・ホームが現在の`app.log`しか読まず、日次のローテーション後は前日以前のログが出ない | `LogReaderService.read_entries(since=)`で`app.log.YYYY-MM-DD`も日付順に読む。ホームの直近アクティビティは直近7日分（ファイル名の日付の翌日未明の行が含まれ得るため1日余裕を見る） |
| GU-7 | ログ画面が表示時に読み直さず、期間の終了日も起動した日のまま。常駐していると最近の検知が出ない | `LogScreen.on_shown()`を画面を開くたびに呼ぶ。終了日が前回の「今日」のままなら同じ日数の期間のまま今日へ進める（利用者が変えた期間はそのまま） |
| SC-1 | スキャンの停止操作（ソケットの打ち切り）やclamdの再起動でFILDESの応答が空になると「非対応」と判断し、アプリを再起動するまでFILDESを使わない（以後は権限エラー→INSTREAMの2回通信、100MB超は検査不可）。停止時はSCANを送り直すため停止もすぐに効かない | `ClamdClient.scan_fd`は、応答を受け取れて「fd[」で始まらない場合だけ`FdPassUnsupportedError`にする。応答が無い場合は`ClamdConnectionError`（スキャンの停止中なら表示しない既存の扱い）。送信に失敗しても、非対応のclamdは応答を返して閉じるため、応答を読んでから判断する |

テスト: 上記の各項目にテストを追加した（未実行。別PCで実行する）。起動確認のみ。

実機で確かめる項目（PRの確認観点に記載）:
- PK-1: 管理者権限が必要なexeをRunキーに登録しているため、Windowsのログオン時に自動起動しない（UACも出ない）可能性。Windows 11実機で、自動起動を有効にしてサインインし直して確認する。起動しない場合はタスクスケジューラ（最上位の特権で実行）方式を検討する
- GU-1: システムトレイが使えない環境では、最小化で起動するとウィンドウもトレイも出ない。別PCのWSL（WSLg）はトレイ（StatusNotifier等）を持たないため、トレイが無い環境として確認に使える可能性がある。`QSystemTrayIcon.isSystemTrayAvailable()`がFalseになることを確かめたうえで、起動時の挙動を確認する

残っている課題（今回は直さない。次回、修正するか検討する）:
- レビューで「直さなくてよい」とした項目。報告ページの指摘のうち上の8件と実機確認の2件を除くもの（重要度 高: RT-1・PK-2・TS-1・TS-2、ほか中・低）。主なもの
  - PK-2: 管理者権限で動く本体が、一般ユーザーが書き換えられるconfig.jsonの`update.freshclam_path`を実行する。アプリのフォルダが一般ユーザーで書き換えられる配置では、同じフォルダのDLLの差し替えでも同じことができるため、配置方針の課題としてまとめて扱う（要セキュリティ責任者の判断）
  - QS-8: 伏せ字版の診断レポートでfreshclamのパスを伏せていない（伏せる対象はソケット・設定ファイル・clamonaccのパスと決めたため、方針どおり）
  - DB-1・DB-2・DB-5: config.jsonを手で書き換えた場合の型違い・BOM付きUTF-8・数値の範囲（起動しない、設定が初期化される、instream_chunk_size: 0で検知漏れ）
  - RT-1: 監視対象の外のフォルダもclamonaccの--exclude-listに書く（clamonaccが除外を登録できずE2005になる可能性。Linux実機で要確認）
  - SC-3〜SC-5、SU-3〜SU-11、RT-4〜RT-12、QS-1〜QS-7、DB-3・DB-4・DB-6・DB-8〜DB-11、GU-3〜GU-6・GU-8・GU-9、PK-3〜PK-13、TS-1〜TS-14

## 37. 製品名をClamQtPieに変更

製品名を「ClamQt」から「ClamQtPie」に変え、旧名（ClamAVフロントエンド / ClamAVFrontend）が残っていた箇所もそろえた（利用者と方針を確認済み）。

| 対象 | 変更 |
|---|---|
| 表示名 | `core/version.py`の`APP_NAME`。タイトルバー・ヘッダー・トレイ・診断レポート・メールの件名と本文・テストメール・Linuxの自動起動エントリの`Name=` |
| 配布物 | `ClamQtPie.spec`（旧`ClamAVFrontend.spec`）、`ClamQtPie.exe`・`ClamQtPieActivate.exe`、配布フォルダ`ClamQtPie/`、Zip名`ClamQtPie_v<版>_<OS>.zip`、`version_info.txt`、アイコン`icons/clamQtPie.ico` |
| OSに登録する識別子 | `APP_ID`（`ClamQtPie`）: キーリングのサービス名、自動起動のRunキーの値、トースト通知の差出人（AUMID）、ミューテックス。`APP_SLUG`（`clamqtpie`）: `%LOCALAPPDATA%`・`~/.local/share`のフォルダ、URLスキーム、自動起動の`.desktop`。トーストの環境変数も`CLAMQTPIE_TOAST_*`にした |
| 変えないもの | コードのフォルダ`clamav_frontend/`、ロガー名`clamav_frontend`（どちらも利用者に見えない）、設計書（`docs/`）のファイル名と本文 |

旧名で登録した内容の引き継ぎ（`core/legacy_migration.py`。起動時に毎回確認し、無ければ何もしない。失敗しても起動は止めない）:
- SMTPパスワード: キーリングの`ClamAVFrontend`から`ClamQtPie`へ移す（新しい側に既にあれば上書きせず、旧名の分だけ消す）。`ConfigService.migrate_legacy_smtp_password()`
- 自動起動: Runキーの`ClamAVFrontend`・`clamav-frontend.desktop`は旧版のexeを指すため、消して現在のexeで登録し直す。`AutostartService.migrate_legacy()`
- URLスキーム`clamqt:`: 登録されたコマンドが本アプリの補助プログラム（`ClamAVFrontendActivate.exe`・`notify_activate.py`）の場合だけ消す（他のアプリの登録は消さない）
- `%LOCALAPPDATA%\clamav-frontend`・`~/.local/share/clamav-frontend`: 本アプリのファイル（依頼ファイル・ロックファイル）を消し、空になったら消す

注意点:
- ミューテックス名が変わったため、旧版（ClamAVFrontend.exe）と新しい版を同時に起動すると二重に起動できてしまう。バージョンアップでは古い版を終了してから置き換える（READMEの手順どおり）
- Windowsの通知設定（アプリごとの通知のオン・オフ）は差出人の名前ごとのため、旧名で通知をオフにしていた場合は新しい名前で設定し直す必要がある

確認（Windows、Python 3.15.0 / 3.14.8）: 全テスト成功（引き継ぎのテストを`tests/unit/test_legacy_migration.py`に追加）。この開発機に残っていた`clamqt:`（開発環境の`notify_activate.py`を指す）と`%LOCALAPPDATA%\clamav-frontend`を引き継ぎ処理で消し、新しい`clamqtpie:`のコマンドを実行して`%LOCALAPPDATA%\clamqtpie\show_request`が書かれることを確認。新しい差出人名でのトースト表示を確認

## 38. 初回公開（v0.9.0 ベータ）とLinux配布版の起動不具合の修正

公開リポジトリ（GitHub `babaken/ClamQtPie`）での最初のリリースをベータ版として v0.9.0 にした（利用者と確認済み）。`core/version.py`の`APP_VERSION`、`version_info.txt`、READMEのビルド例を0.9.0にそろえた。

Linux配布版がWaylandのデスクトップで起動しない不具合（v0.9.0のビルド時に発覚。Phase 11の軽量化で入った）:
- 原因: `ClamQtPie.spec`の`_KEEP_QT_PLUGINS`に`platforms/libqwayland.so`だけを残し、Waylandのシェル統合（`wayland-shell-integration/libxdg-shell.so`）を外していた。Qtは`No shell integration named "xdg-shell" found`でWaylandをあきらめてxcbを試し、xcbの依存ライブラリ（`libxcb-cursor0`など）が無い環境では`Could not find the Qt platform plugin`で異常終了する
- 修正: `libxdg-shell.so`・`libwl-shell-plugin.so`、クライアント側のウィンドウ枠（`wayland-decoration-client/libbradient.so`。`libadwaita.so`はQtSvgが要るため除く）、入力メソッド（`platforminputcontexts`のIBus・compose。日本語入力に必要。virtualkeyboardはQuick/Qmlが要るため除く）を残すようにした。Zipは97.6MB→97.7MB
- X11のデスクトップでは`libxcb-cursor0`などのシステムのライブラリが必要（Qt 6.5以降の要件）。READMEの「Linux の準備」に書いた
- 開発時（venvから`python app.py`）はPySide6のプラグインを全部使うため起きず、テストもoffscreenで動くため見つからなかった。配布版は展開してから実際のデスクトップで起動して確かめる

確認（WSL2 Ubuntu 26.04 / WSLg、Python 3.14、PySide6 6.12.0）: Zipを展開して`./ClamQtPie`がWaylandで起動し、ログに「アプリ起動シーケンス完了」が出ることを確認。WSLgにはSecret ServiceとStatusNotifierWatcherが無いため、keyringの警告とトレイのD-Busエラーが出るが、環境によるもの。Windows版はspecの変更が`.so`だけのため影響なし

## 39. セキュリティの改善（v0.9.1 に向けて）

本体（`core/`）の安全性を見直し、次の点を改善した。サービス化（今後の版）でも同じ `core/` を使う。

| 対象 | 変更 |
|---|---|
| スキャン結果 | 1ファイルの `SCAN` の結果は、clamd の応答に含まれるパスではなく、検査を依頼したパスの結果として扱う。応答が1件の結果として解釈できなければ ERROR（`ClamAVService._bind_to_requested_path`）。`ScanWorker` の通知・ログも列挙したパスで行う |
| Linux のリアルタイム監視（clamonacc） | root シェルが root 専用の一時フォルダ（`mktemp -d`）にログ・監視リスト・除外リストを置き、ログを `tail -F` で標準出力へ流す。アプリはそのパイプだけを読む。リストは引数で渡す（改行を含むパスは渡さない）。検知の行は、監視対象の配下にあることを確かめ、`ClamAVService` で検査し直して FOUND の場合だけ隔離する（検査し直せなければ通知のみ）。終了マーカーに起動ごとの識別子を付けた。`ClamonaccRealtimeManager` の引数に `clamav_service` を追加し、`work_dir` をなくした |
| 隔離・復元 | ファイル移動をリンク（ジャンクション・シンボリックリンク）をたどらずに行う（新規 `core/safe_fs.py`）。Windows は `FILE_FLAG_OPEN_REPARSE_POINT` で開いて実際のパスを確かめ、ハンドルに対して名前を変える。Linux 等はルートから1段ずつ `O_NOFOLLOW` で開いたフォルダの fd を基準に操作する。復元先に既にあるものがリンク・フォルダなら上書きの指定があっても置き換えない |
| 外部コマンド | 絶対パスで起動する（新規 `core/exec_path.py`）。Windows の `sc`・`powershell` は `GetSystemDirectoryW` のフォルダ、Linux の `systemctl` 等は決まったディレクトリから探す。freshclam（Windows）は ClamAV の導入先 → PATH の絶対パスの項目の順に探し、カレントディレクトリは探さない |
| 実行ファイルの場所 | 管理者・root で実行する freshclam・clamonacc と clamonacc の設定ファイルは、実行の直前に、絶対パス・想定どおりの名前・ファイルと親フォルダに管理者（root）以外が書き込めないことを確かめ、満たさなければ実行しない（新しいエラーコード E6005 `UnsafeExecutablePathError`）。clamd のサービス名は安全な形だけを受け付ける |
| 多重起動防止 | ミューテックス（Linux はロックファイル）を取れないとき、同じ実行ファイルの別のプロセスが動いているかを確かめ、動いていなければ WARNING を記録して起動を続ける |
| ログ・通知 | ファイル名などの制御文字（改行など）を `\n` のような見える形に置き換える（新規 `core/text_safety.py`。ログのフィルタで一括、検知の通知・メールにも適用）。Linux の notify-send の本文はエスケープする。ログの CSV 出力で数式として扱われる値の先頭に `'` を付ける |
| clamd との通信 | 応答の受信を 1 MiB までにした |
| SMTP | 暗号化が「none」のときは認証しない（パスワードを平文で送らない）。設定画面に注意書きを追加 |
| 検査できなかったファイル | ERROR の理由（サイズ等の上限・権限・削除済み・その他）を分類して画面に表示し、件数と完了時の注意を出す。スケジュールの最終実行結果に「未検査 N件」。clamd の `Heuristics.Limits.Exceeded.*`（`AlertExceedsMax yes` のときの「上限を超えた」報告）は検知ではなく、上限を超えて未検査として扱う（大きいだけのファイルを自動で隔離しない）。診断で `AlertExceedsMax` が無効なら注意を出す |
| 定義データベースの古さ | 新しい設定 `update.stale_days`（既定3日）より古ければ、ホーム画面に警告、トレイのツールチップに注意、古くなった時点で1回通知（新規 `core/definitions.py`） |
| README | Windows では管理者だけが書き込める場所（`C:\Program Files\ClamQtPie\` など）に展開し、一般ユーザーが書き込める場所・USB メモリ・同期フォルダを避けるよう案内。Microsoft Defender との併用と、脆弱性の報告方法（SECURITY.md）を追記 |

動作の変わる点:
- ClamAV を一般ユーザーが書き込める場所に置いている場合、定義更新・clamonacc の起動が E6005 で止まる（管理者だけが書き込める場所へ置き直す）
- SMTP の暗号化を「none」にして認証が必要なサーバーを使っている場合、メールが届かなくなる（ssl か starttls にする）
- 開発時（`python app.py`）は、他の `python.exe` のプロセスがあると、従来どおり多重起動として終了することがある（配布版は実行ファイルのフルパスで判定する）

確認（Windows 11、Python 3.15.1 / WSL2 Ubuntu、Python 3.14）: 全テスト成功（Windows 506件、Linux 512件）。WSL の実機 clamonacc（root）で `tests/integration/test_real_clamonacc.py` 成功。この開発機の freshclam（Chocolatey で導入）が場所の検証を通ることを確認

## 40. セキュリティの改善（v0.9.1 に向けて、続き）

§39 の改善の後の見直しで、次の点を改善した。

| 対象 | 変更 |
|---|---|
| 外部コマンドの環境変数（Windows） | 管理者権限で起動する外部コマンド（通知の PowerShell、sc、freshclam）には、本体の環境変数を引き継がず、動作に必要な決まった変数だけを Windows の API から得た値で渡す（新規 `exec_path.child_env`）。Linux 等では従来どおり引き継ぐ（root で動かすものは pkexec が環境変数を整理する） |
| pkexec（Linux） | `which` で PATH から探すのをやめ、決まったディレクトリの絶対パスで起動する（新規 `exec_path.find_system_tool`。clamd の起動・停止、clamonacc、定義更新） |
| 実行ファイルの場所（Linux） | root で実行・読み込むファイル（freshclam・clamonacc・clamonacc の設定ファイル）は、ファイルと親のフォルダに加えて、`/` までのすべての祖先のフォルダについても、所有者が root で root 以外が書き込めないことを確かめる |
| 画面の表示 | ファイル名・clamd の応答・ログのメッセージを表示するラベル・ダイアログは `PlainText`、ツールチップは `Qt.convertFromPlainText` で表示し、HTML として解釈させない（新規 `gui/plain_text.py`。隔離ファイルの詳細は `QMessageBox` のインスタンスで表示するように変更） |
| 定義の日付 | clamd の応答の日付が範囲外の値でも例外にせず「不明」として扱い、状態の表示の更新を続ける |
| テスト | Linux 専用の監視シェル（`_SUPERVISOR_SCRIPT`）を実際に動かすテストは、POSIX の環境だけで実行する（Windows の sh ではプロセスの停止の挙動が異なり、結果が安定しないため） |

動作の変わる点:
- Windows: 定義更新の freshclam は、一時フォルダとして `%SystemRoot%\Temp` を使う（本体は管理者権限のため書き込める）。freshclam・PowerShell に、利用者が設定した環境変数（プロキシの設定など）は渡らない。freshclam のプロキシは `freshclam.conf` の `HTTPProxyServer` 等で設定する
- Linux: pkexec が決まったディレクトリ（`/usr/bin` 等）に無い環境では、clamonacc 方式は使えず watchdog になる。ClamAV を、利用者が書き込めるフォルダの下（root 所有のフォルダでも）に置いている場合は、E6005 で止まる

確認（Windows 11、Python 3.15.1 / WSL2 Ubuntu、Python 3.14）: 全テスト成功。この開発機で、決まった環境変数だけで通知（PowerShell のトースト）・`sc query`・`freshclam --version` が動くこと、WSL の `/usr/sbin/clamonacc`・`/usr/bin/freshclam`・`/etc/clamav/clamd.conf` が場所の検証を通ることを確認

## 41. v0.9.1 の公開

§39・§40 のセキュリティの改善を v0.9.1 として公開する。`core/version.py`の`APP_VERSION`、`version_info.txt`、READMEと`build_release.py`のビルド例を0.9.1にそろえた。

確認: 全テスト（Windows 11 / WSL2 Ubuntu）成功。実機で次を確認した。
- Windows（管理者で起動）: 定義の手動更新、検知の通知（通知センターに届き、クリックで隔離画面が開く）
- Linux（実機のデスクトップ、一般ユーザーで起動）: 起動（監視方式は clamonacc）、pkexec での定義の手動更新とリアルタイム監視の開始、検知の隔離と notify-send の通知、カスタムスキャンでの検知、監視の停止と終了後に clamonacc が残らないこと

この確認で見つかった次の点は、v0.9.2 以降で対応する: notify-send の通知にアプリ名・アイコンが付かない、clamonacc のログが2回記録される、glibc 2.44 以降の環境では clamonacc がサブフォルダを監視できない（ClamAV Issue #1797）ことの案内、定義更新の結果の文言の画面とログの違い、終了時のログ行が無い。

## 42. v0.9.1 の実機確認で見つかった点の修正（v0.9.2 に向けて）

§41 の確認で見つかった点を直した。
- Linux の通知（notify-send）に、アプリ名（`--app-name`、`APP_NAME`）とアイコン（`icons/app.png`）を付ける。これまで `app.py` がアイコンを渡しておらず、通知の差出人が「notify-send」と表示されていた
- clamonacc のログが2回記録されていた。`--foreground` の clamonacc はログと同じ内容を画面にも出し、それが監視用のシェルの `tail -F` と同じパイプに流れていた。clamonacc の画面への出力は root 専用の一時フォルダのファイルに受け、終了後にログに無い行（ログを開く前のエラー、画面にだけ出る `Clamonacc: stopped` など）だけを流す（`grep -vxFf`）
- 終了の確認ダイアログの「リアルタイム監視も停止されます」は、監視中のときだけ出す（監視を止めた後にも出て、監視中と誤解させていた）
- 定義更新の履歴の結果「変更なし」を、ログ・通知と同じ「最新」にする
- 終了時に「アプリ終了シーケンス開始」「アプリ終了シーケンス完了（終了コード）」をログに書く（正常に終了したかを調査で判断できるように）
- 実 clamonacc の結合テストのうち除外フォルダのテストは、glibc 2.44 以降では ClamAV Issue #1797 で必ず失敗するため、`strict=False` の xfail にする（README の既知の問題には記載済み）

## 43. watchdog 7.0 への対応

2026-10-31 に公開された watchdog 7.0.0 で、Windows のテスト（`test_realtime_manager.py` の 4 件）が CI で失敗した。7.0.0 の Windows の監視（`WindowsApiEmitter`／`DirectoryChangeReader.get_events(timeout)`）は、`timeout`（既定 1 秒）の間に新しいイベントが来なくなるまで待ってからまとめて渡すため、ファイルの変更から検知まで約 1 秒遅れていた（6.0.0 では即座に届く。イベントは失われない）。テストがそれより短い時間しか待たないため失敗していた。Linux（inotify）では差が出ない。

`RealtimeManager` で `Observer(timeout=0.1)`（`_OBSERVER_TIMEOUT_SEC`）にして、7.0.0 でも約 0.1 秒で届くようにした。6 系では待ちの間隔が変わるだけで、届くまでの時間は変わらない。版は固定しない（`watchdog>=4.0` のまま）。確認: watchdog 6.0.0・7.0.0 の両方で、Windows・WSL の全テスト成功。v0.9.1 の配布版は 6.0.0 でビルドしている。

## 44. v0.9.2 の公開

§42・§43 の修正と開発環境からの起動スクリプト（リポジトリ直下の `scripts/`）を v0.9.2 として公開する。`core/version.py`の`APP_VERSION`、`version_info.txt`、READMEと`build_release.py`のビルド例を0.9.2にそろえた。

確認: 全テスト（Windows 11 / WSL2 Ubuntu / Linux 実機）成功。Linux 実機（glibc 2.44、watchdog 7.0.0）で、通知の差出人とアイコン、clamonacc のログが1回ずつであること、停止の行、定義更新の履歴の文言、watchdog 方式での検知、終了ログと終了確認の文言を確認した。

## 45. 段階 12-1: Web API（読み取り）

サービス化・Web API設計書の段階 12-1。従来のアプリ（`app.py`）の中で内部 API を動かし、curl 等で読めるようにした。既定は OFF で、`config.json` の `"api": {"enabled": true}` のときだけ待ち受ける（OFF のときは FastAPI・uvicorn を読み込まない）。

構成（`api/`）:
- `app.py`: `create_app(tokens, context)`。ルーターは `/api/v1` の下。`/docs`・`/redoc`・`/openapi.json` は作らない
- `errors.py`: `AppError` を `{"error": {code, message, detail}}` で返す。状態コードは例外の種類で決める（401 E8101、403 E8102/E8103/E8108、404 E3003、409 E8104/E4004、429 E8106、400 検証エラー、503 E1xxx、500 それ以外）。500 は detail・例外の文字列を応答に含めずログにだけ残す。入力の検証エラーは 400（E8100）で、どの項目かだけを返し、入力された値は返さない
- `server.py`: 127.0.0.1 だけで待ち受けるソケットを自分で作って uvicorn に渡し、専用のスレッドで動かす。Windows は `SO_EXCLUSIVEADDRUSE`。サーバー名を名乗らず（`server_header=False`）、`X-Forwarded-For` 等で接続元を書き換えさせない（`proxy_headers=False`）。ポートを使えなくてもアプリは続ける
- `auth.py`: user・admin の Bearer トークン（待ち受けを始めるたびに `secrets.token_urlsafe(32)`、`hmac.compare_digest`）。トークンファイルは `api_tokens/`（アプリのフォルダ。コードの `api/` と混ざらない名前）に、本人（Windows は加えて SYSTEM・Administrators）だけが読める権限で書き出し、待ち受けを止めたら消す。フォルダがリンクなら書き出さない。すべての要求で、接続元が 127.0.0.1／::1、Host が 127.0.0.1／localhost とこのポート、状態を変えるメソッドでは Origin・Referer（付いていれば）がこのPC のものであることを確かめる
- `bridge.py`: `QtInvoker`（任意のスレッドから処理をメインスレッドで実行させる）、`EventHub`（イベントの配信。連番・直近 1,000 件・追いつかない購読者は切り離す）。12-2 の状態を変える API・SSE の土台で、12-1 ではまだ使わない
- `routes/read.py`: `/status`、`/quarantine`、`/excludes`、`/realtime`、`/realtime/targets`、`/schedules`、`/updates/history`、`/logs`、`/logs/export.csv`（すべて user 以上。一覧は `?limit=` 既定 100・最大 1000、`?offset=`）

設計書と異なる点・決めた点:
- トークンファイルの置き場所は、サービス化（12-3）まではアプリのフォルダの `api_tokens/`。読めるのは本人だけ（設計書の `ClamQtPie Users` グループに読ませる形は 12-3 で行う）
- Host の検査に失敗したときは 400（3.1節に合わせた。4.2節の表は 403）。接続元・Origin の失敗は 403
- 状態を変えるメソッドの Origin・Referer は、付いていない要求（GUI・curl）は通す。付いていてこのPC 以外なら拒否する
- `/status` は `clamd.endpoint`・`clamd.verified`（接続先の確認、9.5節）と `update.next_run` を返さない（それぞれ未実装・12-2 以降）
- `/updates/current`（ジョブ）は 12-2
- 500 になる `AppError` も detail を返さない（設計書は予期しない例外だけを明記）
- ログの時刻はこのPC のローカル時刻にタイムゾーンを付けて返す

依存: `fastapi`・`uvicorn`（テスト用に `httpx2`。Starlette 1.10 の TestClient は旧来の `httpx` を非推奨にした）。配布版は PyInstaller の標準のフック（`hook-uvicorn`）で取り込まれ、`ClamQtPie.spec` の変更は要らなかった。Zip は Windows 39.9→44.3 MB、Linux 97.7→102.0 MB。

確認: 全テスト（Windows・WSL）成功。WSL で配布版をビルドし、API を有効にして起動して、トークンファイルの権限（700/600）、`/version`・`/status`・`/logs`・CSV、トークンなしの 401、Host 違いの 400、`/docs` の 404、127.0.0.1 だけで待ち受けることを curl で確認した。

## 46. 段階 12-2a: Web API（状態を変える操作）

サービス化・Web API設計書の段階 12-2 のうち、状態を変える API と SQLite の WAL 化。12-2 は大きいため 4 つに分けた（12-2a: 状態を変える API、12-2b: ジョブの API と SSE、12-2c: スキャン履歴、12-2d: 隔離の所有者の復帰と隔離ファイルの変換）。

構成:
- `api/actions.py`: 状態を変える処理。`QtInvoker` でメインスレッドに渡して実行する（実行中かどうかの判定もメインスレッドで行い、画面・API・スケジュールの同時操作の取り合いを防ぐ）。中身は画面の操作と同じにし、変えた後は `ApiContext.notify_changed` で `app.py` に知らせて画面を読み直させる。変えた内容は `Web API: …` としてログに残す（設定は値を残さず、変えた項目の名前だけ）
- `api/routes/write.py`（すべて admin）:
  - 隔離: `POST /quarantine/{id}/restore`（`{"overwrite": bool}`。元の場所にだけ復元する。同じ名前があり上書きしないなら 400 E3002）、`DELETE /quarantine/{id}`
  - 除外: `POST /excludes`、`PATCH /excludes/{id}`、`DELETE /excludes/{id}`（ファイル・フォルダは絶対パス、拡張子は画面と同じく小文字・先頭に `.`、重複は 400）
  - 監視: `PUT /realtime`（`{"enabled": bool}`。有効な監視対象が無ければ 400）、`POST/PATCH/DELETE /realtime/targets`（存在するフォルダを絶対パスで）
  - スケジュール: `POST /schedules`、`PUT/PATCH/DELETE /schedules/{id}`、`POST /schedules/{id}/run`（202。スケジュール画面の「今すぐ実行」と同じく別のスレッドで実行。実行中なら 409）。スキャンの対象は絶対パス
  - 設定: `PUT /config`（全体を置き換える）、`PUT/DELETE /config/smtp-password`、`POST /notifications/test-mail`
  - clamd・診断・ログ: `POST /clamd/start`（202。試行中なら 409）、`POST /clamd/stop`（停止検知時の自動起動の抑止を含め、エンジン サービス画面と同じ）、`POST /diagnostics`（伏せ字にしたレポート）、`DELETE /logs`
- `GET /config`（user）: API 自身の設定（`api`）を除き、SMTP のパスワードは `smtp.has_password` で有無だけ返す。Web API で変えられない項目を `protected` に並べる
- 入力は余計な項目・型の違う値（`"true"`・`1` を真偽値として扱うなど）を受け付けない（400 E8100）
- 見つからない除外・監視対象・スケジュールは 404 E8109（`ApiNotFoundError`、新設）。隔離データは従来どおり E3003
- `db/database.py`: 接続ごとに `PRAGMA journal_mode=WAL`・`busy_timeout=5000`。WAL の付随ファイル（`-wal`・`-shm`）は SQLite が DB ファイルと同じ権限（POSIX は 0600）で作る
- `api/server.py`: 待ち受けの停止をログに残す。uvicorn の警告・エラー（不正な HTTP の要求など）をアプリのログに書く（制御文字は見える形にする）

設計書と異なる点・決めた点:
- **Web API で変えられる設定は、設定画面で変えられる項目（と `update.stale_days`）に限る。** 設計書の保護された項目（実行ファイルの場所、clamd の接続先、サービス名）に加え、`clamd.tcp_port` も変えられない（ポートも接続先の一部のため。設計書の一覧に漏れていた）。画面に無い項目（タイムアウト・同時接続数など）と `general`（画面ごとの設定。12-3 で利用者ごとの GUI の設定に移す）も読み取りだけ。範囲外の値は読み込み時のように直さず 400 にし、項目の名前を返す
- 設定は、現在の設定のオブジェクトをその場で書き換えてから保存する（各サービスは起動時に受け取った設定の部分を持ち続けているため。設定画面と同じ）
- 復元・削除済みの隔離データへの復元・削除は 400
- 隔離の「同じ名前がある」は `QuarantineRestoreConflictError`（E3002 のまま）で区別し、400 にする。ほかの復元の失敗は 500
- スケジュールのスキャンの中止を admin に限ること（設計書 6.2）は、スキャンのジョブの API とあわせて 12-2b
- `/schedules` の `wake` 列（スリープからの復帰）は 12-3

見つけた不具合: WAL にすると、アプリのフォルダを監視したとき、DB に書くたび（隔離の記録など）に `app.db-wal` が変わって検査され、検知の扱いによっては隔離されてしまう（WSL のテストで発見。従来も `app.db` 自体が検査されていた）。watchdog 方式では、`realtime_factory` がアプリの DB のファイル（`app.db`・`-wal`・`-shm`・`-journal`）も検査から外すようにした（clamonacc の `--exclude-list` はフォルダを指定するもので、ファイルを渡すと監視の設定に失敗するため、clamonacc には渡さない）。

確認: 全テスト（Windows・WSL）成功。WSL で実際のアプリ（ソースから、offscreen）を API を有効にして起動し、user での 403、除外・監視対象の追加、監視の ON/OFF、スケジュールの追加と今すぐ実行、設定の保存（`config.json` への反映）と保護された項目の拒否、診断を curl で確認した。DB が WAL になることも確認した。

## 47. 段階 12-2b: Web API（ジョブとイベント配信）

サービス化・Web API設計書 4.3節・5章・6.2節・6.7節。スキャンと定義の更新をジョブとして扱い、イベントを SSE で配る。

構成:
- `api/jobs.py`（`JobTracker`）: `ScanDispatcher`・`UpdateDispatcher` のシグナルからジョブ（`id`・`kind`・`label`・`source`（`gui`|`api`）・`state`（`running`→`finished`|`cancelled`|`failed`）・`started_at`・`finished_at`・`progress`・`error`）を作り、イベントを出す。画面・トレイから始めたものも記録する（両 dispatcher に開始のシグナル `started` を足した）。終わったジョブは種類ごとに直近 1 件
- `api/events.py`: `realtime.detected`・`realtime.handled`・`realtime.target_lost`・`realtime.error`、`status`（ホーム画面の定期確認の結果から、変わったときだけ）、`config.changed`（画面・Web API のどちらで保存しても）
- `api/routes/jobs.py`（すべて user）: `POST /scans`（202。`type`: `quick`|`full`|`custom`、`targets`、`include_subfolders`、`apply_exclude`。実行中は 409）、`GET /scans/current`、`POST /scans/current/cancel`、`POST /updates`（202。実行中は 409）、`GET /updates/current`、`GET /events`（`text/event-stream`。連番の `id`、`Last-Event-ID` より後を送り直す。15 秒ごとに空のコメント。同時の購読は 16 まで）
- Web API から始めたスキャン・更新は、スキャン画面・定義更新画面も実行中の表示にする（`show_started_elsewhere`）
- イベントに入れるファイル名・メッセージ・freshclam の出力は、制御文字を見える形にする
- uvicorn は止めるとき、つながったままの購読を 1 秒で切る（`timeout_graceful_shutdown`）

設計書と異なる点・決めた点:
- クイックスキャンで `targets` を省いたときは、画面と同じ場所（`quick_scan_targets()`）を使う。アプリが利用者の権限で動く 12-2 までの扱いで、12-3 からは設定 `scan.quick_targets` に変える
- スケジュールのスキャン・更新は dispatcher を通らない（APScheduler のスレッドで直接実行）ため、ジョブにならず、Web API から中止もできない。そのため「スケジュールから始めたスキャンの中止は admin」は 12-3（スケジュールの実行をジョブにまとめるとき）に行う。`schedule.finished` イベントも 12-3
- `scan.activity` の内容は `{job_id, state, path, seconds}`（`ScanWorker.activity` のまま。`state`: `queued`|`stalled`|`scanning`）
- `status` イベントは `GET /status` の一部（clamd の到達性、定義の古さ、監視、スキャン・更新の実行中とジョブの id）。全部が要るときは `GET /status` を読む
- `scan.started`・`update.started` を追加した（ジョブの内容をそのまま送る）
- `notification`・`notification.acked` は通知の保存（12-3）とあわせて行う
- `PUT /realtime`（12-2a）の `{"running": true}` は「開始を受け付けた」の意味。clamonacc 方式では pkexec の認証の前に返り、認証のキャンセル・起動の失敗は後から `realtime.error` で、状態が戻ったことは `status` で届く（Linux 実機の確認で指摘。方式で応答の形を変えると使う側が分かりにくいため、200 のまま説明する）

確認: 全テスト（Windows・WSL）成功。WSL で実際のアプリを API を有効にして起動し、`/events` を購読しながら `POST /scans`（custom）・`POST /updates` を呼び、ジョブが記録されて `scan.started`・`scan.progress`・`scan.finished`・`status`・`update.started` が届くことを確認した。

## 48. 段階 12-2c: スキャン履歴

サービス化・Web API設計書 6.2節。手動（画面・トレイ・Web API）とスケジュールのすべてのスキャンを、終了時に記録する。

構成:
- Alembic `0004_add_scan_history`: `scan_history`（`started_at`・`finished_at`・`source`（`gui`|`api`|`schedule`）・`scan_type`（`quick`|`full`|`custom`|`schedule`）・`label`・`targets`（JSON）・`scanned`・`detected`・`errors`・`errors_truncated`・`result`（`completed`|`cancelled`|`failed`）・`detail`）、`scan_history_detection`（`path`・`virus_name`・`action`（`quarantined`|`notified`））、`scan_history_error`（`path`・`reason`（`size_limit`|`permission`|`vanished`|`other`）・`detail`）
- `core/scan_history_service.py`: `ScanHistoryService`（記録・一覧・詳細・保持期間を過ぎたものの削除）、`ScanHistoryCollector`（スキャン中の結果を集める。検査できなかったファイルは 1,000 件まで、超えた分は件数だけ）、`ScanHistoryRecorder`（`ScanDispatcher` のシグナルから手動のスキャンを記録）
- `ScanDispatcher.start` に `source`・`scan_type` を足した（スキャン画面・Web API が渡す）
- スケジュールのスキャン（`run_scheduled_scan`）は、成功・失敗（対象が無い、clamd に接続できない）とも記録する。検知の `action` は検知時アクションの結果
- 起動時に `logging.retention_days` を過ぎた履歴を消す（SQLite の外部キーの連鎖削除は有効にしていないため、子の行も明示して消す）
- Web API（user）: `GET /scans/history`（`?source=&since=&limit=&offset=`、新しい順）、`GET /scans/history/{id}`（`detections`・`error_files` を含む。無ければ 404 E8109）。`scan.finished` とジョブに `history_id` を付ける
- `realtime.error` に `reason` を足した（`auth_cancelled`・`exited`・`monitoring_failed`・`start_failed`・`not_found`・`config`・`clamd_unavailable`・`target`・`unsafe_path`・`other`）。detail（clamonacc の出力の一部・パス）は送らない（Linux 実機の確認で指摘）

見つけた不具合: 1ファイルのエラー（応答待ちのタイムアウトなど。スキャンは続く）でも、Web API のジョブを `failed` にしていた（12-2b）。clamd に接続できない・対象の列挙に失敗した、のときだけ失敗にする（`is_fatal_scan_error`）。履歴も同じ判定で、1ファイルのエラーは直前に検査を始めたファイルのエラーとして残す。

設計書と異なる点・決めた点:
- マイグレーションの番号は 0004（設計書の案では 0005。0004 の予定だった通知の保存（12-3）より先に作るため。12-3 の分は 0005 以降にする）
- 詳細の検査できなかったファイルの一覧は `error_files`（件数の `errors` と名前が重ならないように）
- 履歴の一覧の画面（トレイGUI のスキャン画面）は、GUI を API のクライアントにする 12-4 で作る

確認: 全テスト（Windows・WSL）成功。WSL で実際のアプリから Web API でスキャンし、ジョブの `history_id`、`/scans/history`・`/scans/history/{id}` を確認した。

## 49. 段階 12-2 の修正（Linux 実機の確認から）

- **イベントの購読（SSE）がつながったままアプリを終了すると、ログに ERROR と Traceback が残り、終了が約 1 秒遅れていた**（PR #15 の確認で発見。12-2b から）。待ち受けを止めるとき、uvicorn が購読の処理を打ち切っていた（`timeout_graceful_shutdown`）。止める前に `EventHub.end_all()` で全部の購読に終わりの印（`END`）を送り、配信を自分で終わらせるようにした
- Web API のカスタムスキャンの名前を、画面と同じ「カスタムスキャン」にした（従来は「手動スキャン」。履歴の label が経路で違っていた）
- 名前の付け方の違い（変えない。互換のため）: スケジュールの `scan_type` は `scan`・`update`（ジョブの種類）、`POST /scans` の `type` は `quick`・`full`・`custom`（スキャンの範囲）、スキャン履歴の `scan_type` は `quick`・`full`・`custom`・`schedule`

## 50. Windows のスキャンが遅かった件（clamd への接続の方法）

Rust 化の事前検証（`feat/rust-poc`）で見つかった、Python 版の問題。`socket.create_connection(..., timeout=...)` は、Windows では1回の接続ごとに時間がかかる。スキャンは1ファイルに1回接続するため、件数が多いと大きく遅くなっていた。

- Windows で接続先がこのPC 自身（127.0.0.1・::1・localhost。既定の接続先）のときは、タイムアウトを付けずに接続し、つないだ後で読み取りのタイムアウトを設定する（`core/clamd_client.py` の `_connect_without_timeout`）
- 別のマシンへの接続と Linux は従来どおり（タイムアウトを付けないと、届かないアドレスで約 21 秒待つ。従来は 5 秒）

測った値（Windows 11、本物の clamd、127.0.0.1:3310）:

| 場面 | 修正前 | 修正後 |
|---|---|---|
| 接続だけ（中央値） | 0.90 ms | 0.11 ms |
| 1ファイルの検査（2,000 件、2 回） | 18.5 ms・11.9 ms | 1.4 ms・3.6 ms |
| clamd が止まっている（127.0.0.1） | 約 2.0 秒で失敗 | 約 2.0 秒で失敗（変わらない。Windows がループバックへの接続を数回やり直すため） |
| clamd が止まっている（localhost） | 約 4.1 秒 | 約 4.1 秒（変わらない） |
| 届かないアドレス（別のマシン） | 5.0 秒 | 5.0 秒（従来どおりの方法のまま） |

1ファイルの検査は、もう一つの作業で PC の負荷が揺れていたため値がばらついたが、3〜13 倍速くなった。

## 51. UTF-8 として正しくない名前のファイルの扱い

Linux のファイル名は UTF-8 として正しくないバイト列でもよい。そうした名前のファイルがあると、TCP 接続（clamd にパスを送る `SCAN`）のスキャンが途中で止まり、画面も「スキャン中」のまま戻らなかった（Rust 化の事前検証で発見）。

- clamd に送るパスは、OS のファイル名の元のバイト列のまま送る（`surrogateescape`）
- それでも送れない名前（Windows の対になっていないサロゲート）は、アプリが開いて内容を送る（`INSTREAM`）方法で検査する（`ClamdPathEncodingError`）
- スキャンの処理は、1ファイルの想定外の失敗でも止めず、そのファイルのエラーとして続ける
- ログ・イベント・スキャン履歴などへの書き出しでは、こうした名前の文字（対になっていないサロゲート）を `\udcff` のような見える形にする（`visible_controls`。そのままではファイル・JSON・DB に書けない）

- **パスを渡す Qt のシグナルは `object` にした**（`ScanWorker`・`ScanDispatcher` の `progress`・`file_scanned`・`activity`、リアルタイム監視の `detected`・`detection_handled`・`watch_target_lost`）。`Signal(str)` では、Qt の文字列に変わるときにこうした名前の文字が黙って消え、受け取った側（スキャン履歴・イベント・スキャン画面の一覧）で別のファイルの名前になっていた（Linux 実機の確認で発見。PySide6 6.12）
- スキャン画面の一覧は見える形で表示し、隔離・無視には表示の文字ではなく、検査したファイルのパスそのものを使う（従来は表示の文字で隔離していたため、同じに見える別のファイルを隔離するおそれがあった）
- 隔離の記録（DB）に書けない名前は、ファイルを動かす前に断る（E3001）
- clamonacc の出力は `surrogateescape` で読む（`errors="replace"` では読んだ時点で名前が置き換え文字になり、検査し直せずに通知だけになっていた。Linux 実機の確認で発見）
- notify-send に渡す文字はバックスラッシュを二重にする（notify-send は `\n` などの並びを解釈するため、見える形の `\udcff` のバックスラッシュが消え、`reportudcff.txt` のような、ありそうな別の名前に見えていた）
- スキャン画面で隔離できなかったときも、どのファイルかをログに残す

残すもの: こうした名前のファイルは隔離の記録に書けないため、隔離できずに通知だけになる。隔離の保存の形を変える 12-2d で扱う。

## 52. 配布版が Python 3.15 で起動の直後に落ちていた件

Python 3.15.2・PyInstaller 6.22.3 で作った配布版が、起動の直後に APScheduler のところで `TypeError: 'lazy_import' object is not callable` で落ちていた。

- 原因: Python 3.15 の `concurrent.futures` は `ThreadPoolExecutor` 等を遅延 import（PEP 810）する。PySide6 を読み込んだ後では、`concurrent.futures.ThreadPoolExecutor` が遅延 import の印のまま返る。従来の対策（app.py の最初、PySide6 より前に属性を読んで実体化する）は、配布版では PySide6 の runtime hook が本体より先に PySide6 を読み込むため間に合わなかった（PySide6 を先に読み込む小さなプログラムを配布版にして確認した）
- 対策: `core/lazy_import_fix.py` の `reify_concurrent_futures()` で、下位のモジュール（`concurrent.futures.thread`・`process`）から本物のクラスを読み込み、`concurrent.futures` の名前を置き換える（読み込みの順番に関係なく効く）。app.py と `tests/conftest.py` の最初に呼ぶ
- テスト: PySide6 を先に読み込んだ別のプロセスで、APScheduler が起動できること
- 確認: Windows で配布版（UAC を求めない確認用の写し）を作り、起動シーケンスの完了まで進むことを確認した

## 53. 段階 12-2d: 隔離ファイルの変換・所有者の復帰

サービス化・Web API設計書 6.3節（12章 45、S-11）と、所有者の復帰。あわせて §51 の残り（UTF-8 にできない名前のファイルを隔離できない）を直した。

- **隔離ファイルの変換**（`core/quarantine_codec.py`）: 隔離したファイルは、先頭に識別用のヘッダ（`CQPQ`・形式の番号・元の大きさ・元の SHA-256）を付け、中身を固定の鍵で XOR して保存する（他のウイルス対策ソフトに検知・削除されたり、誤って実行されたりしないように。暗号としての強さが目的ではなく、鍵は秘密にしない）。隔離フォルダへ移した元のファイル（`<uuid>.moving`）を変換してから消す
- **復元**: 元に戻したファイル（`<uuid>.restoring`）の大きさ・SHA-256 が記録と一致しなければ、復元せずに E3005（`QuarantineCorruptedError`、新設）。一致すれば従来どおり `safe_fs.restore_move` で元の場所に置く
- **所有者・権限**（`core/file_owner.py`）: 隔離するときに記録し、復元したときに戻す（Linux は uid・gid・mode。所有者は root のときだけ戻し、setuid・setgid は戻さない。Windows は SDDL の所有者・DACL。所有者を変える権限が無ければ DACL だけ）。戻せなくても復元は成功とし、ログに残す
- **UTF-8 にできない名前**: DB の文字列に書けないため、元のバイト列を `original_path_raw` に持ち、`original_path` は見える形にした表示用にする。復元はバイト列から元のパスに戻して行う（§51 で入れた「隔離の前に断る」はやめた）
- Alembic `0005_quarantine_hardening`: `quarantine` に `encoding`（既定 `none`）・`owner_info`・`original_path_raw`。以前の版で隔離したファイルは `none` のまま扱い、変換し直さない（移行の失敗で隔離ファイルを失わないため）

設計書と異なる点・決めた点:
- マイグレーションは設計書の案の 0007（所有者）・0008（変換）を 1 つにまとめ、0005 にした（12-3 の通知の保存・スケジュールの `wake` は 0006 以降）
- 記録の無い以前の隔離データの復元は、所有者・権限に手を付けない（設計書の「復元先フォルダの継承 ACL・所有者に合わせる」は、サービス化（12-3）で SYSTEM・root が復元するときに行う）

残すもの: 隔離・復元の途中でアプリが落ちると、隔離フォルダに途中のファイル（`.moving`・`.restoring`。元の内容）が残ることがある（読み取りは本人だけ）。

確認: 全テスト（Windows・WSL）成功。Windows・Linux とも、UTF-8 にできない名前のファイル（Windows は対になっていないサロゲート、Linux は正しくないバイト列）を実際に作って隔離・復元し、同じに見える別のファイルが変わらないことを確かめた。

## 54. v0.9.3 の公開

v0.9.2 の後の修正（§49〜§53 のうち、Web API に関わらないもの: Windows のスキャンの高速化 §50、UTF-8 として正しくない名前のファイルの扱い §51・§53、Python 3.15 の配布版の起動 §52、リアルタイム監視（clamonacc）を起動の直後に止めても確実に止まるようにした修正）と、サービス化・Web API の段階 12-1〜12-2（§45〜§49。既定は OFF で、`config.json` の `"api": {"enabled": true}` のときだけ待ち受ける）を v0.9.3 として公開する。`core/version.py` の `APP_VERSION`、`version_info.txt`、README と `build_release.py` のビルド例を 0.9.3 にそろえた。

隔離の形式（§53）を変えたため、v0.9.3 で隔離したファイルは v0.9.2 以前では復元できない（以前の版で隔離したファイルは v0.9.3 でそのまま復元できる）。DB は起動時に 0005 まで上がる。

## 55. 段階 12-3a: サービスの本体

サービス化・Web API設計書 9章。画面を持たず、監視・スキャン・スケジュール・定義の更新・隔離と内部 API を動かすサービスを作った。12-3 は 4 つに分ける（12-3a: サービスの本体、12-3b: 管理用コマンドと設定、12-3c: スケジュールと通知、12-3d: 接続先の確認と残り）。

構成:
- `service_main.py`: `--console [--data-dir DIR]`（前面で動かす。開発中の確認用で登録は要らない）、`--systemd`（Linux。unit は `Type=notify`）、`install`・`remove`・`start`・`stop`（Windows サービスの登録・操作。pywin32 の `HandleCommandLine`）。配布版が SCM から起動されたときは `StartServiceCtrlDispatcher`
- `core/runtime.py`: 画面に関わらないサービス一式を組み立てる（従来の `app.py` の起動の順番から画面に関わるものを除いたもの）。従来の `app.py` は、画面がサービスのクライアントになる 12-4 まで自分で組み立てる
- `api/service_api.py`: サービスの内部 API。`config.api.enabled` に関係なく待ち受け、待ち受けられなければ起動を失敗にする（終了コード 3）。稼働状態のイベント（status）は 2 秒ごと、clamd への問い合わせは別のスレッドで 60 秒ごと
- `core/service_paths.py`: データの置き場所（Windows `%ProgramData%\ClamQtPie\`、Linux 設定 `/etc/clamqtpie/`・データ `/var/lib/clamqtpie/`・ログ `/var/log/clamqtpie/`）。無ければ本人・SYSTEM・Administrators／root だけが使える権限で作る。サービスとして動くときは起動のたびに所有者と権限を確かめ、一般の利用者が書き込めるフォルダ・リンクがあれば起動しない（終了コード 2。`C:\ProgramData` の直下を先に作られた場合も含む）。`--console` では警告だけ
- `core/systemd_notify.py`: 内部 API が待ち受けを始めてから `READY=1`、止めるときに `STOPPING=1`（libsystemd を使わず `NOTIFY_SOCKET` に送る）。Windows は待ち受けの後に `SERVICE_RUNNING` を報告する
- SIGTERM・SIGINT で終了の処理を通って止まる（トークンファイルも消える）。`--console` はデータの置き場所のロック（`service.lock`）で二重起動を防ぐ（終了コード 4）。サービスとしての二重起動の防止は SCM・systemd に任せる
- 起動の行に版を出す（`サービスの起動シーケンス開始（ClamQtPie 0.9.3）`）
- `core/service_status.py`・`app.py`: 従来のアプリは、サービスが動いていれば警告を出して起動しない（監視・スケジュールが二重に動かないように。12-4 までの扱い）
- `scripts/install_service.sh`・`uninstall_service.sh`（`sudo` で systemd に登録・削除。`--purge` で設定・データ・ログも消す）、`scripts/install_service.bat`・`uninstall_service.bat`（UAC で昇格して Windows サービスに登録・削除）。開発中の確認用で、配布物には入れない。サービスは SYSTEM・root で、開発中のソースのフォルダのコードを実行するため、確認が終わったら必ず削除する

設計書と異なる点・決めた点:
- トークンファイルは置き場所の `api/` に、SYSTEM・Administrators／root だけが読める権限で置く。`user.token` を `ClamQtPie Users` グループに読ませるのは、グループを作るインストーラ（12-4）とあわせて行う
- サービスはデスクトップに通知を出せない（Windows はセッション 0）。メールの通知は従来どおり送る。画面に知らせる仕組み（通知の保存）は 12-3c
- `status` イベントの定義の古さは、サービスでは 12-3d で出す（今は `null`）

確認: 全テスト（Windows・WSL）成功。Windows・WSL で `--console` で動かし、API に応答すること、二重起動を防ぐこと、WSL で SIGTERM で止まりトークンファイルが消えることを確認した。Windows サービス・systemd としての登録は、管理者・root の権限が要るため、利用者に scripts のスクリプトで確かめてもらう。

## 56. 段階 12-3b（前半）: 管理用コマンドと秘密の保存先

サービス化・Web API設計書 3.4節・6.8節。12-3b は「コマンドと設定」（この節）と「移行」（`migrate`、設定の分割）に分ける。

- `clamqtpie_ctl.py`（`clamqtpie-ctl`。root・管理者で実行）:
  - `status`: サービス・内部 API・clamd の状態、置き場所
  - `config show`、`config set KEY VALUE [--yes] [--restart]`: Web API からは変えられない項目（実行ファイルの場所、clamd の接続先、`api.port` など）を含めて変える。変更の前後の値を表示して確認を求める。値は今の値の型で読み、範囲外は受け付けない。実行ファイル・設定ファイルの場所は、設定するときにも確かめる（絶対パス、管理者・root だけが書き込める場所、想定どおりの名前）。サービスが動いている間は変えない（サービスが設定を保存するときに上書きされるため）。`--restart` でサービスを止めて変え、また開始する。変えた内容はサービスのログに残す
  - `quarantine restore ID --to PATH [--overwrite]`: 元の場所以外へ復元する（Web API では元の場所にだけ復元できる、6.3節）
  - `--data-dir DIR`: 開発中の確認用（サービスの `--console --data-dir` と同じ置き場所。root でなくてよい）
- `core/secret_store.py`（`FileSecretStore`）: サービスの SMTP パスワードの保存先。SYSTEM・root では keyring を使えないため、置き場所の `secrets/` にファイルで保存する（Windows は DPAPI の PC 単位の鍵で暗号化、Linux は 0600）。従来のアプリは keyring のまま（`ConfigService(secret_store=None)`）
- `core/private_files.py`: 本人・SYSTEM・Administrators／root だけが使えるフォルダ・ファイルの作成（API のトークン・サービスの置き場所・秘密の保存先で共有。`api/auth.py` から移した）
- `scripts/install_service.sh` の unit に `RestartPreventExitStatus=2 3`（置き場所の権限・API のポートの失敗は、起動し直しても直らないため。PR #23 の確認で指摘）

PR #24 の Linux 実機の確認で直したもの:
- `--restart` を続けて使うと、3 回目の開始が systemd の起動の回数の上限（`StartLimitBurst=3`、1 日。設計書 9.4 の回復動作）に掛かり、変更は保存されたのにサービスが止まったままになった。ctl は開始する前に `systemctl reset-failed` で数え直す。開始に失敗したときは、変更は保存済みでサービスが止まっていることと、原因を確かめる場所を表示する
- 実行ファイルの場所（`update.freshclam_path`・`realtime.clamonacc_path`）を、空または既定の名前で「PATH から探す」既定に戻せなかった（空の `clamonacc_path` はそのまま保存されていた）。空なら既定の名前を保存し、既定の名前は設定するときの確認を省く（実行の直前に PATH から探して確かめる）
- 確認の問いで標準入力が閉じていると例外の表示になった。「いいえ」として扱う
- サービスのログに残す行が端末にも表示されていた（`setup_logger(console=False)`）。「開始しられませんでした」の誤字

後の段階に回すもの: `web ...`（WebUI の設定）は 12-5、`users add|remove`（トレイGUI を使える利用者のグループ）はグループを作るインストーラとあわせて 12-4、`migrate` と設定の分割（`gui.json`・`schema_version` 2）は 12-3b の後半。

確認: 全テスト（Windows・WSL）成功。

## 57. 段階 12-3b（後半）: 旧版のデータの移行と設定の分割

サービス化・Web API設計書 6.8節「設定の分割」・9.2節。

- 設定の分割:
  - サービスの設定（`ConfigService(gui_settings=False)`）は `schema_version` 2。利用者ごとの GUI の設定（`general`）を含めない。12-3a で作った `general` 入りの設定は、読み込んだときに 2 へ変える
  - 従来のアプリ（ポータブル）は、これまでどおり `schema_version` 1 で `general` を含む。トレイGUI が `gui.json` を使うようになる 12-4 まで残す
  - `core/gui_config.py`: 利用者ごとの GUI の設定 `gui.json`（Windows は `%APPDATA%\ClamQtPie\`、Linux は `~/.config/clamqtpie/`）。12-3b では移行が書き出すだけ
  - Web API の `GET /config` に `general` を含めず、`PUT /config` でも受け付けない（`HIDDEN_SECTIONS`）。`clamqtpie-ctl config` も扱わない
  - Web API で変えられる項目の一覧（`WRITABLE_FIELDS`）と範囲を `core/config_model.py` に移した（移行でも同じ一覧を使うため）
- `clamqtpie-ctl migrate --from DIR [--yes] [--restart]`（`core/portable_migration.py`）: 旧版のフォルダの設定・DB・隔離ファイル・ログを、サービスの置き場所へ移す。旧版のフォルダは一般の利用者が書き換えられる場所にあるため、中身を信頼しない:
  - ファイルはリンクをたどらずに開き（`safe_fs.open_regular_file`）、リンク・通常でないファイルは移さない
  - 設定は Web API で変えられる項目だけを、型と範囲を確かめて移す。保護された項目は移さず、旧版の値が既定値と違えば最後に一覧を表示する
  - DB はファイルごと置き換えず、旧版の DB の写しから決まった表・列だけを読み、値を確かめて移行先の DB に入れる（隔離ファイルの名前の形、隔離の保存形式、スケジュールの種別・時刻の指定、除外の種類）。隔離したファイルの元の所有者・アクセス権は移さない
  - 除外・監視対象・スケジュール・隔離中のファイルの一覧を表示して確認を求める
  - 移行先に既にデータ（隔離・スケジュール・除外・監視対象・スキャンの履歴）があれば移行しない。旧版が動いている（`app.db-wal` が残っている）ときも移行しない
  - 隔離ファイルは1件ずつ写し、失敗したら写したものを消して中止する。終わってから旧版の隔離ファイルを消し、旧版のフォルダに `MIGRATED.txt` を置く（同じフォルダからもう一度は移さない）
  - SMTP パスワード: Windows は利用者の資格情報ストアから読んでサービスの保存先へ移す。Linux は入力を求める（空なら省略）
  - `gui.json`: 実行した利用者のものに書き出す。Linux で sudo から実行したときは、その利用者の権限に下げた子プロセスで書く。既にあれば上書きしない

PR #25 の Linux 実機の確認で直したもの:
- 旧版の現在のログ（`app.log`）を、最後に書いた日付（旧版を終了したばかりなら今日）の `app.log.<日付>` として写していた。サービスがその日の夜中に今日の分を同じ名前へ切り替えようとすると、Python 3.14 の `TimedRotatingFileHandler` は既にある名前を見て切り替えをやめ、次の切り替えの時刻も進めないため、それ以降ログが切り替わらなくなった（古い Python では、写したログが消される）。今日以降の日付の名前は使わず、昨日の日付にまとめる（同じ名前になるものは古い順につなげる）
- 元の所有者の記録が無い隔離ファイル（旧版から移したもの）を `quarantine restore` で復元したときは、所有者が管理者・root になることと、所有者・アクセス権を変えるよう案内する
- 管理用コマンドで、Alembic の経過の行が端末に出ていた（`DatabaseService(quiet=True)`）

確認: 全テスト（Windows・WSL）成功。sudo から実行したときの `gui.json` の書き出しは Linux 実機で確かめる。

## 58. 段階 12-3c-1: スケジュールの取りこぼし対策と定義更新の分岐

サービス化・Web API設計書 9章・9.1節。12-3c は 3 つに分ける（12-3c-1: 取りこぼし対策と定義更新、12-3c-2: 通知の保存、12-3c-3: スリープからの復帰（`wake`））。

- `SchedulerService`:
  - APScheduler に `coalesce`（重なった回は1回にまとめる）と `misfire_grace_time`（設定の `schedule.misfire_grace_sec`、既定 3600 秒、60〜86400）を指定する。従来のアプリも同じ
  - `catch_up_missed()`: 各ジョブの最終実行日時（`last_run`）と実行周期から、電源オフ・スリープ中に過ぎた回があれば、1 分後に1回だけ実行する（実行周期のジョブは次の回から数え直す）。APScheduler はジョブを覚えていない（メモリだけ）ため、サービスの起動時に呼ぶ。一度も実行していないジョブは対象にしない
  - `on_resume()`: スリープからの復帰時に、取りこぼしを確かめてから APScheduler の待ち時間を計算し直させる（Linux では待ち時間にスリープ中の時間が数えられず、そのままでは次の回がスリープしていた時間だけ遅れる）
  - `inhibit_sleep=True`（サービス）なら、ジョブの実行中はスリープを防ぐ（定義の自動更新も）
  - `on_job_finished`: 最終実行結果を記録したときに呼ぶ。Web API の `schedule.finished` イベント（`api/events.connect_scheduler`）
- `core/resume_watcher.py`: 30 秒ごとに時計を確かめ、想定より 120 秒以上進んでいれば復帰したとみなす。OS の通知（Windows のサービスの電源イベント、Linux の logind）を受け取るより単純で、両方で同じ仕組みになり、依存も増えない（設計書と違う点）
- `core/sleep_inhibit.py`: Windows は `SetThreadExecutionState`（ジョブのスレッドで呼ぶ）、Linux は `systemd-inhibit --what=sleep` で `cat` を動かしておき、標準入力を閉じて終わらせる。できなければ何もしない
- Linux の定義更新: OS の `clamav-freshclam.service` が動いていれば、root で動くサービスでも、無人実行（自動・スケジュール）は行わない（`skipped`。PR #23 の確認で、OS のサービスと重ねて更新していた）。利用者の操作による更新は、従来どおり freshclam を直接実行する（ログは `--log=/dev/null`）。設計書の「OS のサービスを再起動してジャーナルから結果を読む」は、従来の方法で問題が出ていないため行わない（設計書と違う点）

確認: 全テスト（Windows 824 件・WSL 842 件）成功。

## 59. 段階 12-3c-2: 通知の保存

サービス化・Web API設計書 6.10節・7.1節。サービスはデスクトップに通知を出せない（Windows はセッション 0、Linux はログインした利用者のセッションがない）ため、未読として保存し、トレイGUI・WebUI が読んで表示する（表示は段階 12-4・12-5）。

- `pending_notification`（Alembic `0006_add_pending_notification`。設計書の案では 0004 だが、先にスキャン履歴・隔離の変更が入ったため 0006）: `id`、`created_at`、`title`、`body`、`open_screen`、`acked_at`
- `core/notification_store.py`（`NotificationStore`）: 保存・未読の一覧・既読・古い既読の削除。タイトル・本文は制御文字を見える形にし、長さを制限する（200・2000 文字）。`open_screen` は `ALLOWED_SCREENS` のものだけを残す。既読にしたものは `logging.retention_days` を過ぎたら、サービスの起動時に消す（未読は消さない）
- `NotificationService(desktop_sink=...)`: 渡されていれば、デスクトップに表示する代わりに保存する（サービスだけ。従来のアプリは従来どおり表示する）。保存できなくても、メールの通知や検知後の処理は続ける
- サービスの終了のとき、集約の途中（最初の検知から 60 秒以内）の検知通知を、DB を閉じる前に保存する（`NotificationService.flush_pending`。Linux 実機の確認での指摘）
- Web API（user 以上。サービスだけ）:
  - `GET /notifications/pending`: `{"notifications": [...], "total": n}`。未読を新しい順に最大 500 件と、未読の全体の件数
  - `POST /notifications/ack`: `{"ids": [...]}`（最大 1000 件）か `{"all": true}` のどちらか一方。`{"acked": [...]}`（実際に既読にした ID）を返す。既読にした件数・ロール・接続元をログに残す（GUI／WebUI の区別は、WebUI の認証が入る段階 12-5 で加える）
  - イベント: `notification`（`{id, created_at, title, body, open_screen}`）・`notification.acked`（`{ids}`。既読にしたものが無ければ送らない）

確認: 全テスト（Windows 840 件・WSL 858 件）成功。

## 60. 段階 12-3c-3: スケジュールのためにスリープから起こす

サービス化・Web API設計書 6.6節・9.1節。ジョブごとに「スリープ中でも起こして実行する」（`wake`）を選べるようにし、サービスが OS の復帰タイマーを設定する。既定は false（ノート PC が鞄の中などで起動しないように）。編集画面の項目は段階 12-4・12-5。

- `schedule.wake`（Alembic `0007_add_schedule_wake`。設計書の案では 0006）。既存のジョブは false
- `SchedulerService(wake_timer=...)`: `wake` が有効なジョブのうち、次の実行時刻が最も早いものの 2 分前（`WAKE_LEAD`）に、タイマーを1つだけ設定する。ジョブの追加・変更・削除・有効無効の切り替え・実行の後、起動時、スリープからの復帰時に設定し直す（同じ時刻なら設定し直さない）。サービスの停止時に消し、起動時は必ず一度設定し直す（異常終了で残ったタイマーも消す）。設定できなくてもスケジュールは止めない（復帰後に取り返す）。定義の自動更新では起こさない
  - `update_job(wake=None)` は wake を変えない（従来のアプリの編集画面にはまだ項目が無いため）
- `core/wake_timer.py`:
  - Windows: `CreateWaitableTimer`（名前なし）と `SetWaitableTimer(fResume=True)`。タイマーはサービスのプロセスが持ち、終われば消える
  - Linux（root のサービスだけ）: `/etc/systemd/system/clamqtpie-wake.timer`（`OnCalendar=<UTC の日時>`、`WakeSystem=true`）と、何もしない `clamqtpie-wake.service` を書き、`daemon-reload` と `restart` で読み直させる。不要になったら止めて消す。`uninstall_service.sh` も消す
  - 一般の利用者の従来のアプリは起こさない
- Web API: `POST /schedules`・`PUT /schedules/{id}` に `wake`（既定 false。PUT は全体を置き換えるため、送らなければ false）。`GET /schedules` の各ジョブにも `wake`
- `clamqtpie-ctl status`: Windows は電源プランの「スリープ解除タイマーの許可」（`powercfg /query SCHEME_CURRENT SUB_SLEEP RTCWAKE`。表示の言語によらないよう値だけを読む）、Linux は設定中の起こす予定を表示する。診断画面への表示は段階 12-4

確認: 全テスト（Windows 856 件・WSL 873 件）成功。

## 61. 段階 12-3d-1: clamd の接続先の確認

サービス化・Web API設計書 9章（12章 43）。12-3d は 3 つに分ける（12-3d-1: clamd の接続先の確認、12-3d-2: 内部 API の接続先の確認・`endpoint.json`・回復動作、12-3d-3: 残り（隔離の途中のファイルの片づけ、`scan.quick_targets`、定義の古さ、user の手動スキャンの対象））。

clamd が止まっている間に、同じポート・ソケットで別のプログラムが待ち受けていても、検査するファイルの名前・中身を渡さないよう、サービスは接続の前に相手を確かめる（`core/peer_verify.py`）。

- Windows（TCP）: `GetExtendedTcpTable`（待ち受けだけ、PID つき）で、ループバックのそのポートで待ち受けているプロセスがすべて clamd のサービス（`clamd.service_name`、既定 `clamd`）の PID であること。psutil の `net_connections` は Windows で待ち受けの状態を正しく返さないことがあったため使わない
- Linux（TCP）: `/proc/net/tcp`・`tcp6` で待ち受けているソケットの所有者が、すべて root か clamd.conf の `User`（既定 `clamav`）であること
- Linux（UNIX ソケット）: ソケットと親のディレクトリの所有者が root か clamd の利用者で、親のディレクトリにほかの利用者が書き込めないこと
- 確かめた結果は 5 秒使い回し、接続に失敗したら確かめ直す。確かめられなければ接続せず、clamd に接続できない（E1001、理由つき）として扱う
- 確かめられなかった理由は WARNING でログに残す（同じ理由は 1 回だけ。確かめられるようになったら INFO）。clamd に接続できずにスキャンを中断するときも、理由をログに残す（サービスではジョブ・履歴に要約しか残らないため。Linux 実機の確認での指摘）
- `clamd.tcp_host` が別の PC のときは確かめられないため、ログに一度警告して接続する
- `GET /status` の `clamd.verified`（確かめた: true、確かめられない・別の PC: false、従来のアプリ: null）
- 従来のアプリ（利用者の権限）は確かめない（サービスだけ）。診断画面への表示は段階 12-4

確認: 全テスト（Windows 868 件・WSL 884 件）成功。この PC の clamd（Windows のサービス、WSL の UNIX ソケット）を実際に確かめられること（1 回あたり約 0.6 ms）。

## 62. 段階 12-3d-2: 内部 API の接続先の確認・endpoint.json・回復動作

サービス化・Web API設計書 3.1節・9.4節（12章 19・20・22）。

- `core/service_endpoint.py`:
  - サービスは待ち受けたポートを `api/endpoint.json`（`{"port", "pid"}`、トークンと同じ権限）に書く。止まっている間も前回の値を残す
  - `read_endpoint()`: 無い・正しくなければ None（呼び出し側は設定の `api.port` を使う）
  - `verify_service_listener()`: トークンを送る前に、ループバックのそのポートで待ち受けているのがサービスかを確かめる。Windows は待ち受けているプロセスがすべて `ClamQtPie` のサービスの PID であること、Linux は待ち受けているソケットの所有者がすべて root であること（12-3d-1 の `core/peer_verify.py` を使う）。違う・確かめられなければ E8105（`ServiceUnavailableError`）
- `clamqtpie-ctl status`: `endpoint.json` のポートを表示し、確かめてから問い合わせる。確かめられなければ問い合わせない。トレイGUI（段階 12-4）も同じ関数を使う
- 回復動作:
  - Windows（`scripts/install_service.bat`）: `sc failure`（60 秒後に起動し直す、2 回まで、1 日で数え直す）と `sc failureflag 1`（エラーで止まったときも対象）
  - Linux（`scripts/install_service.sh`）: 12-3a で設定済み（`Restart=on-failure`、`RestartSec=60`、`StartLimitIntervalSec=1d`、`StartLimitBurst=3`。置き場所の権限・API のポートの失敗は起動し直さない）
  - 正式なインストーラーへの組み込みは段階 12-4

確認: 全テスト（Windows 883 件・WSL 899 件）成功。

## 63. 段階 12-3d-3: 隔離の途中のファイルの片づけ・クイックスキャンの対象・定義の古さ

サービス化・Web API設計書 6.1節・6.2節。user のトークンでの手動スキャンの対象（R-9）は 12-3d-4 に分けた。

- 隔離の途中で中断したファイル（§54 の残り）: `QuarantineService.recover_interrupted()` をサービス・従来のアプリの起動時に呼ぶ
  - `<uuid>.restoring`（復元の途中で元に戻した中身。隔離ファイルは残っている）は消す
  - `<uuid>.moving`（隔離の途中で元の場所から移した中身。まだ記録が無い）は、元の内容のまま置かないよう変換し、元の場所が分からない隔離の記録（ウイルス名「（不明: 隔離の途中で中断）」）として残す。誤検知だった場合に失わないため。元の場所が分からないため、復元は復元先を指定したときだけ（`clamqtpie-ctl quarantine restore --to`）
  - リンク・通常のファイル以外・名前の形が違うものには触らない
- `scan.quick_targets`（新設、既定は空、絶対パス 100 件まで）: サービスで、Web API で対象を指定しないクイックスキャン（ブラウザ・ctl）が使う場所。空の間は 400。従来のアプリの API は今までどおり利用者のフォルダを使う（`ApiContext.service_mode` で分ける）。Web API（`PUT /config`）と `clamqtpie-ctl config set scan.quick_targets '["/path"]'`（JSON の配列）で変えられる。設定画面の項目は段階 12-4・12-5
- 定義の古さ（サービス）: 60 秒ごとの clamd の確認で、`VERSION` の定義の日付から経過日数を求め、`status` イベントの `definitions` に出す（これまでは `null`）。`update.stale_days` を超えた時点で1回、通知（保存）とメール（`notification.on_update_fail` と `on_detect_mail` が有効な場合）で知らせる。clamd が応答しない間は判定を変えない。従来のアプリはメールを送らない（今までどおり）

確認: 全テスト（Windows 902 件・WSL 919 件）成功。

## 64. 段階 12-3d-4: user のトークンのスキャンを、頼んだ利用者のフォルダの中に限る

サービスは SYSTEM・root で動き全員のファイルを読めるため、user のトークンで任意の場所をスキャンできると、ほかの利用者のフォルダのファイル名と検知の有無が分かってしまう。そこでサービスでは、user のトークンのスキャンを、頼んだ利用者のプロファイル・ホームの中に限る（利用者の判断。設計書の「クライアントの偽装でのアクセス確認・SO_PEERCRED」の代わりに、接続元のソケットの持ち主で判断する）。

- `core/peer_identity.py`: 内部 API への接続（ループバックの TCP）の接続元のポートから、相手のソケットを OS の表で探して利用者を求める
  - Windows: `GetExtendedTcpTable`（接続中、PID つき）→ プロセスの利用者（SID）→ `ProfileList` の `ProfileImagePath`
  - Linux: `/proc/net/tcp`・`tcp6` の接続元の行の uid → ホーム
- `POST /scans`（サービス、user のトークン）:
  - 対象を指定したスキャン（`custom`、対象を指定した `quick`）は、すべての対象が頼んだ利用者のフォルダの中（リンクをたどった実際の場所で判断）のときだけ。利用者を割り出せないとき・外を含むときは 403（E8102）
  - フルスキャンは admin だけ。対象を指定しないクイックスキャン（管理者が決めた `scan.quick_targets`）はそのまま
  - admin のトークン、従来のアプリの API（利用者の権限で動く）は限らない
- スキャンの列挙（`ScanWorker`）は、リンク・ジャンクションをたどらない（すべてのスキャン）。`os.walk` は Windows のジャンクションをたどり、一般の利用者でもほかの利用者のフォルダを指すジャンクションを自分のフォルダに作れるため（実際に確かめた）。リンクの先が対象の中にあれば、そこを列挙したときに検査する
- トレイGUI（段階 12-4）: クイックスキャン（その利用者のフォルダ）は user のトークンのまま、フルスキャン・ほかの場所は昇格して admin のトークンで頼む

確認: 全テスト（Windows 912 件・WSL 929 件）成功。実際の接続から、テストを動かす利用者のプロファイル・ホームを割り出せること（Windows・WSL）。

## 65. 段階 12-4a: トレイGUI の API クライアントの土台

サービス化・Web API設計書 3.1節・3.2節・5章・10章。12-4 は 5 つに分ける（12-4a: API クライアントの土台、12-4b: 画面のつなぎ替え（user の操作）、12-4c: 昇格ヘルパーと admin の操作、12-4d: 配布物・インストーラ、12-4e: 従来のアプリの削除）。決めたこと: 従来のアプリ（ポータブル）は最終的にソースも含めて削除する、`POST /quarantine` を作る、通信は QtNetwork、Windows のインストーラは Inno Setup（利用者の判断）。

- `api_client/client.py`（`ApiClient`）:
  - 接続先は `api/endpoint.json` のポート（無ければ既定の 8765）。トークンは `api/user.token`。読めなければ E8110（`ServicePermissionError`、新設。「この PC の ClamQtPie を使う権限がありません」）、無ければ E8105（サービスが動いていない）
  - トークンを送る前に毎回、待ち受けているのがサービスかを確かめる（`core/service_endpoint.verify_service_listener`）
  - `QNetworkAccessManager` で送り、結果は `ApiReply.finished(data, error)`。エラーの応答はコードに合う `AppError` の派生にする。つながらない・待ちきれないときは E8105。401（サービスが起動し直してトークンが変わった）はトークンを読み直して 1 回だけやり直す
  - `check_version()`: `GET /version` の `api_version` が合わなければ E8107
  - `ApiReply` はクライアントへの参照を持つ（呼び出し側がクライアントを持っていないと、応答を待つ間にガベージコレクションで消えてプロセスが落ちた）
- `api_client/events.py`（`EventStream`）: `GET /events` を受け取った分ずつ読み、`event(name, data)` を出す。ヘッダーが届いた時点で `connected`。切れたら `disconnected` を 1 回だけ出し、`Last-Event-ID` を付けてつなぎ直す
- トレイGUI の多重起動防止のミューテックスを `Local\ClamQtPie_Tray`（ログオンのセッションごと）にできるようにした（`SingleInstanceLock(mutex_name=TRAY_MUTEX_NAME)`。使うのは 12-4b）
- 12-4d への申し送り: `endpoint.json` を `user.token` と同じくグループに読ませる。spec に QtNetwork を加える（TLS のプラグインは入れない）

確認: 全テスト（Windows 926 件・WSL 942 件）成功。本物の API（uvicorn）につないで、要求・エラー・トークンの読み直し・版数の確認・イベントの受信と再接続を確かめた。

## 66. 段階 12-4b-1: トレイGUI をサービスのクライアントにする（画面のつなぎ替え）

サービス化・Web API設計書 10章。12-4b は 2 つに分ける（12-4b-1: プロキシとトレイGUI の入口・画面のつなぎ替え、12-4b-2: 通知・スキャン履歴の画面・診断画面の追加の表示）。

- `tray_main.py`（新設）: トレイGUI の入口。一般の利用者の権限で動き、サービスの内部 API だけを使う
  - `user.token` を読めなければ E8110 と `clamqtpie-ctl users add <利用者>` を案内して終わる。サービスにつながらない・版数が合わないときは、トレイをエラーの状態にして 5 秒ごとにつなぎ直し、つながったら画面を作る
  - 多重起動防止は `Local\ClamQtPie_Tray`（ログオンのセッションごと）
  - 全 10 画面を、従来のサービスの代わりにプロキシにつなぐ（`app.py` と同じつなぎ方）。サービスのイベント（`config.changed`・`status`・`schedule.finished`）で画面を読み直す
  - ログは利用者ごとの場所（`%LOCALAPPDATA%\clamqtpie\logs`・`~/.local/share/clamqtpie/logs`）
- `api_client/sync.py`（`SyncCaller`）: 画面はこれまでどおり同期的にメソッドを呼ぶ。画面のスレッドからは QEventLoop を回して待ち、別のスレッド（`BackgroundTask` など）からは画面のスレッドに通信を頼んで待つ。一覧は 1,000 件ずつ全部読む
- `api_client/proxies.py`: 従来のサービスと同じ名前・形のプロキシ（設定、隔離、除外、監視対象、スケジュール、更新履歴、clamd の状態、ログ、リアルタイム監視、スキャン、定義の更新）。スキャン・定義の更新・リアルタイム監視のシグナルはイベントから作る。`GET /status` は 2 秒使い回す
  - 設定: サービスの設定は `GET /config`、利用者ごとの GUI の設定（`general`）は `gui.json`。保存では `gui.json` を書き、サービスの設定は変えたときだけ `PUT /config` を送る（トレイの案内の表示済みなどを、一般の権限で保存できる）
  - スケジュールの編集で `wake` を渡さないとき（編集画面に項目が無い間）は、今の値のまま送る
  - admin の操作もプロキシから API を呼ぶ。12-4c で昇格ヘルパーを通すまでは 403（E8102）のエラーを表示する
- `POST /quarantine`（新設、利用者の判断）: スキャン画面の「隔離」。サービスで user のトークンなら、頼んだ利用者のフォルダの中のファイルだけ（§64 と同じ判断）
- ホーム画面の直近のアクティビティは、ログを読めないとき（サービスにつながらない）に前回の表示を残す
- テストの不備を直した: §61 の接続の失敗のテストは、空いているポートを閉じてから使っていたため、全体を流すとほかのテストのサーバーに同じポートを使われることがあった

Linux 実機の確認（#34）で見つかった点を直した:
- サービスにつながらない間、トレイのツールチップがすぐに「ClamQtPie」に戻り、ホーム画面が「clamd: 停止」と案内していた（ホーム画面の定期的な状態の確認で、clamd が止まったと判断していた）。つながらない間は clamd に問い合わせず、ホーム画面に「ClamQtPie サービスにつながりません」を出す。つながらないことのログは、エラーコードが変わったときだけ残す（5 秒ごとのつなぎ直しのたびには残さない）
- スキャン画面の「スキャン件数」が、問題の無いファイルでは数えられず 0 のままだった（サービスは検知・エラーのファイルの結果だけを送る）。進み具合と終わりの件数で数える
- トレイGUI の画面から頼んだスキャンを、スキャン履歴の実行元「画面」（`gui`）にする（`POST /scans` の `source`。表示の区別だけに使う）。クイックスキャンは、画面が求めた場所のうち自分のフォルダの中だけを送る（Linux の `/tmp` などはサービスが user のトークンでは断るため）
- サービスは、権限が足りずに断った要求（403、E8102）をログに残す（エラーのダイアログの「詳細を見る」で見つかるように）
- スキャン結果の検知日時・定義の更新履歴の実行日時を、この PC の時刻で表示する（`gui/time_text.py`。UTC の ISO 形式のまま出ていた）
- 権限が無くて終わるときも、トレイGUI のログに残す

確認: 全テスト（Windows 936 件・WSL 952 件、2 回ずつ）成功。本物の API（uvicorn）にプロキシをつなぎ、読み取り・admin の操作・隔離と復元・GUI の設定の保存・スキャンとリアルタイム監視のイベント・別のスレッドからの呼び出し・トレイGUI の全画面の組み立てを確かめた。導入したサービスでの実機の確認は 12-4b-2 の後にまとめて行う。

## 67. 段階 12-4b-2: トレイGUI の通知・スキャン履歴・診断画面のサービスの状態

サービス化・Web API設計書 7.2節・10章、§48・§60・§61 の残り。

- **通知**（`gui/tray_notifications.py`）: サービスが保存した未読の通知（§59）を、トレイGUI がこの利用者のデスクトップに出す
  - サービスにつながったとき（起動時・つなぎ直したとき）に `GET /notifications/pending`。まだ出していない通知が 2 件以上なら「未読の通知が n 件あります」の 1 件にまとめる。動いている間の通知は `notification` イベントで 1 件ずつ出す。出した通知の ID はメモリに持ち、二度出さない（起動し直したら、未読をもう一度まとめて出す）
  - 既読: トーストのクリックで開いた画面の通知（Windows。`core/activation.py` の仕組みはそのまま。URL に通知の ID は入れない）と、トレイのメニュー「未読の通知（n 件）」の一覧（`gui/notification_dialog.py`）の「選んだ通知を既読にする」「すべて既読にする」で `POST /notifications/ack`
  - `notification.acked`（ほかのトレイGUI・WebUI で既読にしたものも届く）で、通知センターに残っている通知を消す。Windows: Toast に `Tag`（通知の ID）と `Group`（`clamqtpie`）を付けて出し、`History.Remove` で消す（この PC で、消えることを確かめた）。Linux: `notify-send --print-id` の番号を覚え、`gdbus` で `CloseNotification` を呼ぶ。まとめた通知は、含む通知がすべて既読になってから消す
  - 表示・削除（PowerShell・notify-send）は時間がかかるため、1 本の別のスレッドで順に行う。失敗してもログに残すだけ
  - `core/notification_service.py` の表示の処理を関数（`show_windows_toast`・`remove_windows_toast`・`show_linux_notification`・`close_linux_notification`）にして、サービス・従来のアプリと共有する（Tag・番号は渡したときだけ）
- **スキャン履歴**: スキャン画面に「スキャン履歴」の一覧（新しい順に 200 件、§48 の残り）。「詳細」（ダブルクリック）で対象・検知したファイル・検査できなかったファイルを表示する。スキャンの終了・スケジュールの実行・画面を開いたときに読み直す。従来のアプリでは表示しない
- **診断画面**: 「サービスの状態」に、clamd であることを確かめてから接続しているか（§61。`GET /status` の `clamd.verified`）と、スリープから起こす設定（§60。Windows は電源プランのスリープ解除タイマー、Linux は起こす予定）を出す。画面を開いたときに確かめる。ヘルスチェック（`POST /diagnostics`）は admin のため、12-4c までは 403
- **見つけた不具合**: 日本語の Windows で `clamqtpie-ctl status` の電源プランの表示が「確認できません」になっていた（§60）。powercfg は OEM コードページ（cp932）で出力するが、UTF-8 として読んでいた（Python 3.15 は UTF-8 モードが既定）。sc と同じく OEM コードページで読むようにした（`console_text_kwargs`）

Linux 実機の確認（#35）で見つかった点を直した:
- 未読の件数がトレイのメニューを開かないと分からなかった。ツールチップにも出す
- まとめた通知の本文がタイトルだけで、同じ種類の通知（ジョブの失敗など）を見分けられなかった。本文の最初の行を添える
- スキャン履歴の詳細の検知したファイルの扱い（隔離しました・通知のみ）は、スキャンの時点のものであることを書き添える（後でスキャン画面から隔離しても「通知のみ」のまま）

確認: 全テスト（Windows 953 件・WSL 969 件、2 回ずつ）成功。本物の API（uvicorn）につないで、未読のまとめ・イベントでの表示・画面ごとの既読・すべて既読・通知センターから消す処理と、スキャン履歴の一覧・詳細を確かめた。Windows で、Tag を付けた Toast が `History.Remove` で通知センターから消えることを確かめた。導入したサービスでの実機の確認は、12-4c・12-4d の後にまとめて行う。

## 68. 段階 12-4c-1: 昇格ヘルパーで admin の操作を実行する

サービス化・Web API設計書 3.2節・10章（12章 23）。12-4c は 2 つに分ける（12-4c-1: 昇格ヘルパーと、admin の操作をヘルパーに回す仕組み、12-4c-2: 除外・監視対象・スケジュールの画面の「変更を保存」でのまとめ、保護された設定の読み取り専用の表示、`scan.quick_targets`・スケジュールの `wake` の入力欄）。

- **昇格ヘルパー**（`elevate_main.py`。配布版は Windows `ClamQtPieElevate.exe`（`uac_admin=True`）、Linux `clamqtpie-elevate`。12-4d で作る）
  - 操作の一覧（メソッド・パス・本文、最大 200 件）を受け取り、すべてが管理者の操作の一覧（`api_client/admin_ops.py` の `ADMIN_ROUTES`）にあることを確かめる。一覧に無い操作があれば、確認を求めずに断る（admin のトークンをほかの API に送らない）
  - `admin.token` を自分で読み、接続先がサービスであることを確かめてから送る（`verify_service_listener`）。場所は頼んだ側から受け取らない
  - 確認画面に、受け取った操作から自分で作った説明を表示する（`describe`。隔離ファイルの名前・除外・監視対象・スケジュールの名前は、admin のトークンで読んだ一覧から引く。設定の変更は今の値との違いを並べる）。「実行」のときだけ順に実行し、途中で失敗したら残りは実行しない。既定のボタンは「キャンセル」
  - 結果（応答の JSON・実行できた件数・エラー）だけを返す。admin のトークンは返さない。確認画面を飛ばす仕組み（環境変数・引数）は作らない
- **受け渡し**（`api_client/elevation.py`）。「長さ（4 バイト）＋JSON」
  - Windows: UAC で昇格したプロセス（`ShellExecuteEx` の runas）には標準入出力をつなげないため、名前付きパイプにした（設計書は標準入出力）。パイプはトレイGUI が作る（名前は毎回の乱数、最初の 1 つだけ、リモートからは不可）。つないできたのが起動したヘルパーかをプロセス ID で確かめる。ヘルパーは `SECURITY_IDENTIFICATION` でつなぐ（パイプを作った側が、管理者の権限になりすませないように）
  - Linux: `pkexec` で起動し、標準入出力で受け渡す。pkexec の取り消し（終了コード 126・127）は取り消しとして扱う。開発時は確認画面の表示先（`DISPLAY`・`XAUTHORITY`）を `env` で渡す。配布版は polkit のポリシー（`allow_gui`）で渡す（12-4d）
- **admin の操作の回し方**: `ElevatedCaller`（`SyncCaller` と同じ形）を全部のプロキシに渡す。admin の操作は昇格ヘルパーに回し、user の操作が 403（E8102）になったら、一覧にある操作（フルスキャン・自分のフォルダの外のスキャン・隔離・スケジュールのスキャンの中止）だけ 1 回昇格してやり直す。隔離画面の複数選んだ削除は 1 回にまとめる（`delete_many`）
- 取り消し（UAC・pkexec・確認画面）は E8111（`ElevationCancelledError`）。エラーのダイアログは出さない。ヘルパーを起動できない・応答が正しくないときは E8112（`ElevationError`）
- 確認画面を同じ利用者のほかのプロセスから操作されないのは、Windows（UIPI。昇格したプロセスの画面）だけ。Linux の X11・XWayland には画面の間の隔離が無いため、確認画面は誤操作を防ぐためのもので、守りの中心は polkit の認証（`auth_admin`）になる

確認: 全テスト（Windows 973 件・WSL 985 件、2 回ずつ）成功。本物の API（uvicorn）に対して、確認の後の実行・取り消し・途中の失敗で残りを実行しないこと・一覧に無い操作を断ること・接続先の確認を、Windows では名前付きパイプでの受け渡しを含めて確かめた。UAC・pkexec の確認画面は実機で確かめる（12-4d の後）。

## 69. 段階 12-4c-2: admin の画面（変更のまとめ・保護された設定・新しい入力欄）

サービス化・Web API設計書 3.2節・6.8節・10章の残り。§68 の昇格ヘルパーを、画面から 1 回の確認で使えるようにする。

- **変更のまとめ**（`gui/pending_edits.py`）: リアルタイム保護の画面（監視対象・除外・検知時アクション）とスケジュールの画面（追加・編集・削除・有効無効）は、トレイGUI では変更を画面の中にためておき、［変更を保存］で 1 回の昇格にまとめて送る（削除 → 変更 → 有効無効 → 追加 → 設定の順）
  - 一覧の「有効」の列に「追加（保存前）」「削除（保存前）」などを出し、保存していない件数を表示する。［変更を取り消す］で捨てる。削除の予定の行をもう一度削除すると、削除をやめる（保存するまでは取り消せるため、削除の確認は出さない）
  - 昇格の確認で取り消したら、変更はそのまま残す。途中で失敗したら「n 件のうち m 件を保存したところで失敗しました」を出し、一覧をサービスの今の内容に戻す
  - 保存していない変更がある画面を離れる・ウィンドウを閉じるときは、「保存」「破棄」「キャンセル」を確かめる（`MainWindow.confirm_leave`）。トレイのメニューの「終了」では確かめない
  - スケジュールの「今すぐ実行」は、そのジョブに保存していない変更があれば、先に保存するよう案内する。監視への反映はサービスが行う（画面からは `add_target` などを呼ばない）
  - 従来のアプリは、これまでどおりその場で変える
- **スケジュールの編集画面**: トレイGUI では「スリープ中でも起こして実行する」（`wake`、§60）を出す。編集で今の値を引き継ぐ
- **設定画面**（`bind_services(..., service_mode=True)`）
  - 保護された項目（`GET /config` の `protected`。接続の設定・サービス名・clamonacc と freshclam の場所）は読み取り専用にし、入力欄のツールチップで `clamqtpie-ctl config set <項目> <値>` を案内する
  - 「スキャン」タブにクイックスキャンの対象（`scan.quick_targets`、§63）。「定義更新」タブに定義が古いとみなす日数（`update.stale_days`。従来のアプリにも出す）
  - サービスの設定と SMTP のパスワードは 1 回の昇格で保存する（`ConfigProxy.save(model, smtp_password=...)`）。写しを変えて保存し、保存できなければサービスの今の設定を読み直して画面に戻す（#34 の確認で指摘）。取り消したときは入力した値を残す
  - 保存の結果（「保存しました（時刻）」など）を、保存のボタンの横に出す（従来のアプリも）

確認: 全テスト（Windows 993 件・WSL 1005 件、2 回ずつ）成功。本物の API に対して、リアルタイム保護の画面の変更が昇格ヘルパー（同じプロセスで代わりに動かす）を通して保存されることを確かめた。

## 70. 段階 12-4d-1: 配布物の分割・トレイGUI を使える利用者・Linux の導入

サービス化・Web API設計書 3.2節・11章（12章 5・33）。12-4d は 2 つに分ける（12-4d-1: PyInstaller の分割・グループの権限・`clamqtpie-ctl users`・サービスの実行ファイルの置き場所の確認・Linux の `install.sh`／`uninstall.sh`、12-4d-2: Windows のインストーラ（Inno Setup）・`build_release.py`・旧版の自動起動の登録し直し）。

- **PyInstaller の分割**（`ClamQtPie.spec`）: 1 つの spec で、サービス（`ClamQtPieService.exe`／`clamqtpie-service`）・トレイGUI（`ClamQtPie.exe`／`clamqtpie`、**UAC を求めない**）・昇格ヘルパー（`ClamQtPieElevate.exe`（`uac_admin=True`）／`clamqtpie-elevate`）・管理用コマンド（`clamqtpie-ctl`、コンソール）・通知のクリック（`ClamQtPieActivate.exe`、Python のまま）を作り、`_internal/` を共有する。従来のアプリ（`app.py`）は作らない。トレイGUI が使う QtNetwork を入れた（TLS のプラグインは入れない）。uvicorn は文字列で読み込むモジュールを `hiddenimports` に入れた
  - Windows でビルドし、サービス（`--console`）が内部 API の待ち受けまで進むこと、トレイGUI がサービスを待つ状態で動くこと、`clamqtpie-ctl --help` を確かめた（約 128 MB）
- **トレイGUI を使える利用者**（`core/service_access.py`）: サービスは起動のたびに作り直す `user.token`・`endpoint.json` を、ClamQtPie Users（Windows）・`clamqtpie`（Linux）のグループに読ませる（`admin.token` は読ませない）。Linux は `/var/lib/clamqtpie` を `root:clamqtpie` 0710、`api/` を 0750、2 つのファイルを 0640 にする。Windows はファイルにグループの読み取りを足す。グループが無ければ何もしない（開発中の確認用の導入）。できなくてもサービスは止めない
- **`clamqtpie-ctl users list|add|remove <利用者>`**: `net localgroup`／`groupadd`・`usermod -aG`・`gpasswd -d`。グループが無ければ作る。利用者名は決まった文字だけを受け付ける。サインインし直すと使えるようになる
- **サービスの実行ファイルの置き場所の確認**（12章 33）: 配布版のサービスとして動くとき、自分の実行ファイルとそのフォルダを一般の利用者が書き換えられれば起動しない（終了コード 2）
- **Linux の導入**（`packaging/linux/`）: `install.sh`（`/opt/clamqtpie` に写してから入れ替え、`root:root`・`go-w`。リンクなら中止。`clamqtpie` グループを作り `SUDO_USER` を入れる。unit・polkit のポリシー・デスクトップの項目・`/usr/local/bin/clamqtpie-ctl` を置き、サービスを開始する。更新も同じコマンド）、`uninstall.sh`（データを消すかを尋ねる。`--purge` は尋ねずにデータとグループも消す）、`clamqtpie.service`、`io.github.babaken.clamqtpie.policy`（`auth_admin`、`allow_gui`）、`clamqtpie.desktop`。配布物の形（`app/`・`share/`・`install.sh`・`uninstall.sh`）は 12-4d-2 の `build_release.py` で作る
- **見つけた不具合**: 配布版では pywin32 の `pywintypes` が二重に読み込まれ、`except pywintypes.error` で Windows のエラーを捕まえられなかった（グループが無いときにサービスが止まった）。`winerror` の有無で見分ける（`is_windows_error`）。昇格ヘルパーの受け渡しも同じようにした

確認: 全テスト（Windows 1012 件・WSL 1024 件、2 回ずつ）成功。`install.sh`・`uninstall.sh` は構文の確認だけ（Linux の実機での導入は 12-4d-2 の配布物で行う）。

## 71. 段階 12-4d-2: Windows のインストーラ・配布物の作成・旧版の引き継ぎ

サービス化・Web API設計書 11章（12章 32）、12-4 の計画の決めたこと 4（Inno Setup。ACL・グループ・サービスの登録は自前のプログラムで行う）。

- **`build_release.py`**: Windows は PyInstaller の出力から Inno Setup のインストーラ（`ClamQtPie-<版>-setup.exe`）、Linux は tar.gz（`ClamQtPie-<版>-linux64.tar.gz`。`app/`・`share/`・`install.sh`・`uninstall.sh`、すべて root の持ち物、グループ・その他の書き込みなし、実行ファイルは 755）を作り、どちらも `.sha256` を添える。ポータブルの zip は作らない。ISCC は `CLAMQTPIE_ISCC`・`%LOCALAPPDATA%\Programs\Inno Setup 6`・`%ProgramFiles(x86)%\Inno Setup 6` の順に探す。`--no-installer` で PyInstaller の出力まで
- **インストーラ**（`packaging/windows/ClamQtPie.iss`。管理者で動く）: `%ProgramFiles%\ClamQtPie` に置き、`clamqtpie-ctl setup install --user <導入した利用者>` を呼ぶ。選べる作業: サインインしたときにトレイGUI を起動する（`HKCU\...\Run`、引用符で囲む）、`clamqtpie-ctl` を PATH に追加する
  - 更新: 先にサービスを止めて止まるまで待ち、Restart Manager でトレイGUI を閉じて差し替える。終わったらトレイGUI を元の利用者の権限で起動する（`runasoriginaluser`）
  - 旧版（ポータブル）の検出: 自動起動の値が導入先以外の `ClamQtPie.exe` を指していれば、導入の後に移行を勧める（「はい」で `clamqtpie-ctl migrate --from <旧版のフォルダ> --yes --restart`。導入した日のうちに行うよう案内する。§57）
  - 削除: データ（隔離したファイルを含む）を消すかを尋ね（既定は残す）、`clamqtpie-ctl setup uninstall [--purge]` を呼ぶ。PATH から外す
- **`clamqtpie-ctl setup install|uninstall`**（`core/windows_setup.py`。Windows だけ）: データの置き場所（`%ProgramData%\ClamQtPie`）を一般の利用者が作っていた・書き換えられる・リンクなら `ClamQtPie.untrusted-<日時>` に退避して作り直す（12章 32）。ClamQtPie Users グループを作り、導入した利用者を入れる。サービスを止めてから登録（遅延自動起動。既にあれば更新）し、回復動作（§62）を付けて開始する。削除はサービスを止めて登録を消し、`--purge` ならデータとグループも消す
- **配布版のサービスの登録**: pywin32 は配布版でも `pythonservice.exe` を登録しようとするため、サービスのクラスに `_exe_name_`（自分の実行ファイル）を付けた
- **旧版の自動起動の引き継ぎ**: トレイGUI の起動時に、旧版（ポータブルの ClamQtPie・旧名）の自動起動を、このトレイGUI で登録し直す（`AutostartService.refresh_command`・`migrate_legacy`。登録されていなければ登録しない）。通知のクリックの `clamqtpie:` は、トレイGUI が起動のたびに登録し直す
- **見つけた不具合**: トレイGUI が自動起動のコマンドを文字列でなく一覧で渡していたため、設定画面で「OS起動時自動起動」を保存すると失敗していた（12-4b-1 から）。配布版の自動起動のパスを引用符で囲む（`Program Files` の空白）

Windows の実機（開発用の PC）でインストーラを動かして確かめ、見つかった点を直した:
- 更新で、動いているトレイGUI を Restart Manager が閉じられず（ほかのセッションの昇格していないプロセスのため。Permission Denied + Session Mismatch）、`ClamQtPie.exe` を上書きできずに止まった。インストーラが自分でトレイGUI・昇格ヘルパーを終わらせ、`CloseApplications=no` にした。静かな更新でもトレイGUI を起動し直す
- サービスが止まったかの確かめ方を、`sc query` の出力をファイルに書いて読む形にした（止まらなければ導入をやめる）。STOPPED になった後もプロセスが少しの間ファイルを使っていて、削除で使用中のファイルが残ったため、プロセスが終わるまで待つ（10 秒で終わらなければ強制的に終わらせる。インストーラと `setup uninstall` の両方）
- 2 回目の導入で `setup install` が失敗した（既にグループに入っている利用者を `net localgroup /add` するとエラーになる）。入っていれば何もしない。インストーラは失敗のときに ctl の出力（理由）を表示する
- 削除で「データも消すか」の確認が、Inno Setup の削除の確認より先に出ていた。後に出す。前の削除で残ったフォルダも消す（`[UninstallDelete]`）
- 確かめた: 導入（サービスの登録・遅延自動起動・回復動作・グループ・PATH・自動起動）、サインインし直した後に一般の利用者のまま `user.token` を読めて `admin.token` を読めないこと、トレイGUI が昇格せずにサービスの開始を待ってつながること、昇格ヘルパー（UAC → 確認画面 → 実行、UAC の取り消し、確認画面の取り消し）、通知のトーストを既読にすると通知センターから消えること、更新でデータが残ること、削除（データを残す・消す）

Linux の実機（EndeavourOS・KDE Plasma 6 の Wayland・systemd 262）で tar.gz を `install.sh` で導入し、12-4b〜12-4d の全項目が期待どおりであることを確かめた。気になった点を直した:
- 昇格の確認画面: 開いたときに本文の全体が選択された状態だった。カーソルを先頭に置き、フォーカスを「キャンセル」に置く。root で動くため利用者の画面のテーマにならなかったので、トレイGUI と同じ暗色のテーマで表示する（12-4c-1）
- 保護された設定の入力欄が、ほかの欄と同じ見た目で変えられないことが分からなかった。無効の入力欄の背景・文字の色を変える（12-4c-2）
- Plasma 6 では、サインアウトしても利用者の `systemd --user` が止まらず、アプリに新しいグループが付かないことがある（再起動で効いた）。`install.sh`・`clamqtpie-ctl users add` の案内に「使えなければ PC を再起動」を足した。すでにグループに入っている利用者には「入れました」と出さない（12-4d-1・12-4d-2）
- 確認画面の数字の幅が広く見える点は、root の利用者のフォントの設定によるもので、直していない。`systemctl list-timers` の次の実行の表示は systemd の表示の問題（`NextElapseUSecRealtime` は正しい）

確認: 全テスト（Windows 1025 件・WSL 1037 件、2 回ずつ）成功。Windows で `build_release.py` を実行し、インストーラ（約 68 MB）ができることを確かめた（インストーラの実行は、サービス・グループ・PATH を変えるため、利用者の確認を得てから行う）。WSL で tar.gz を作り、中のサービスが起動・終了できることを確かめた。

## 72. 段階 12-4e: 従来のアプリの削除

12-4 の計画の決めたこと 1（2026-11-02）。トレイGUI（§66〜§69）・配布物（§70・§71）がそろったため、従来のアプリ（ポータブル、1 プロセス。GUI が自分でサービスを組み立てる）を削除した。

- **消したもの**: `app.py`（サービスの組み立て・画面への結線・起動の順番）、`scripts/measure_startup_time.py`（`app.py` の起動時間の計測。§30 の計測は従来のアプリのもの）
- **起動スクリプト**: リポジトリ直下の `scripts/run_ClamQtPie.bat`・`.sh` は、開発環境からトレイGUI（`tray_main.py`）を起動するものにした（管理者・root は要らない。先にサービスを動かしておく）。`install_service.*`・`uninstall_service.*`（開発中のサービスの登録）はそのまま
- **設定（schema_version 1）**: `ConfigService` は常にサービスの設定（schema_version 2、`general` を保存しない）として読み書きする（`gui_settings` 引数と `PORTABLE_SCHEMA_VERSION` を除いた）。旧版の schema_version 1 の `config.json` は、読み込むと schema_version 2 として保存し直す（`clamqtpie-ctl migrate` で移した後と同じ）
- **SMTP パスワード**: `ConfigService` は秘密の保存先（`FileSecretStore`）だけを使い、keyring を使わない（保存先が無ければ「なし」として扱い、保存しようとすると E6003 の `KeyringUnavailableError`）。旧名（ClamAVFrontend）の keyring からの引き継ぎ（`migrate_legacy_smtp_password`・`migrate_legacy_names`）は、`clamqtpie-ctl migrate` が旧版のパスワードを読むときに旧名も探すようにした（`portable_migration.legacy_smtp_password`）。自動起動・URL スキーム・旧名のフォルダの引き継ぎは、これまでどおりトレイGUI の起動時に行う（`take_over_legacy_registrations`）
- **画面の、サービスを直接使う経路**: リアルタイム保護・スケジュールの画面は、変更を常に画面の中にためておき［変更を保存］でまとめて送る（`bind_services` の `batch` を必須にした。その場でサービス・監視の本体を変える処理と、削除の前の確認を除いた。監視中の反映はサービスが行う）。設定画面は常にサービスの設定を `ConfigProxy` で読み書きする（`service_mode` 引数を除いた。「スキャン」タブ・保護された項目の読み取り専用・1 回の昇格での保存は常に有効）
- **テスト**: 画面のテストは、`tests/gui_fakes.py` の `FakeServiceApi`（送られた操作を手元の本物のサービスに適用する）と、トレイGUI と同じ `ConfigProxy` で確かめる形に書き直した。従来のアプリだけの動き（keyring への保存、旧名の keyring の引き継ぎ、その場での反映）のテストは除いた
- **残したもの**: サービスが使う `core/`・`db/`、旧版のデータの移行（`clamqtpie-ctl migrate`、`core/portable_migration.py`）、`core/legacy_migration.py` の自動起動・URL スキーム・旧名のフォルダの引き継ぎ
- 利用者向けの README（`README.md`・`README_en.md`）と CLAUDE.md の、従来のアプリを前提にした説明は、12-6（設計書の改訂）でまとめて直す

## 73. 段階 12-5a: WebUI の待ち受けとログイン（ループバックだけ）

サービス化・Web API設計書 3.1節・3.3節・3.4節・6.11節。12-5 の計画（internal の `docs/12-5計画.md`）の 12-5a。画面（`web/`）は 12-5b、トレイのサブメニューは 12-5c、二段階認証は 12-5d、HTTPS と LAN は 12-5e。

- **設定**（`api.*`。すべて保護された項目。`GET/PUT /config` には出さない）: `web_enabled`、`web_port`（8766。内部 API と同じなら直す）、`web_username`（`admin`）、`web_password_hash`（scrypt。空なら未設定で、未設定の間は ON にできない。読み込み時に壊れていれば未設定にして OFF）、`web_password_length`、`web_off_at`（UTC。空なら期限なし）、`session_timeout_min`（30）
- **パスワード**（`core/web_password.py`）: `hashlib.scrypt`（ソルト 16 バイト、n=2^14, r=8, p=1）。照合は `hmac.compare_digest`。ハッシュが無い・ユーザー名が違うときも同じだけ計算する。保存されたハッシュの作り方は n≦2^16 などに限る（設定ファイルを書き換えて照合を重くさせない）。8 文字以上（LAN 許可中は 12 文字。12-5e で使う）
- **待ち受け**（`api/web_control.py` の `WebController`）: 内部 API と同じルーターで `listener="web"` のアプリを作り、2 つ目の `ApiServer`（スレッド名 `WebUI`）で `127.0.0.1:<web_port>` に待ち受ける。ON のときだけ待ち受け、OFF でセッションをすべて破棄して止める。待ち受けに失敗したらサービスは止めずに OFF にして通知する（9.4節）。止めるときは WebUI の SSE の購読だけを終わらせる（`EventHub.end_all(tag)`）
- **認証の切り替え**（`api/auth.py`）: 内部 API は Bearer だけ（Cookie は 401）、WebUI は Cookie セッションだけ（Bearer は 401）。セッションの権限は admin。状態を変える要求は、セッションに結び付けた CSRF トークン（`X-CSRF-Token`、ログイン応答の JSON で渡す。Cookie にはしない）と Origin（無ければ Referer。必須）を確かめる。admin の操作（`require_admin`）は再確認から 5 分以内のセッションだけ（E8108）。`PUT /web`・`PUT /web/credentials` は `require_bearer_admin`（WebUI からは 403 E8102）。接続元はループバックだけ（LAN は 12-5e）
- **ログイン**（`api/routes/web.py`。`/auth/*` は WebUI の待ち受けにだけ付ける）: `POST /auth/session` で新しいセッション ID の Cookie（`HttpOnly`・`SameSite=Strict`・`Path=/`）を発行し、付いていた古いセッションは破棄する。失敗はユーザー名とパスワードのどちらが違ったかを区別しない。同じ接続元から 5 回続けて失敗したら 5 分間 429（E8106）。`DELETE /auth/session` でログアウト、`POST /auth/reauth`（パスワード）で 5 分間「確認済み」。再確認の失敗も同じ回数に数え、ロックしたらセッションも破棄する。ログインの設定を変えたら全セッションを破棄する
- **タイマー OFF**: `PUT /web` の `off_after_min`（1〜527040 分、`null` で期限なし）か `off_at`（ISO 8601、366 日以内）。サービスは 2 秒ごとの状態の確認のときに時刻を比べ、過ぎていれば OFF にして通知する（`NotificationService.notify_system`）。止まっている間に時刻を過ぎていれば、起動時に待ち受けずに OFF にする
- **`clamqtpie-ctl web`**: `status`・`enable [--for D | --until T]`・`timer (D | --until T | clear)`・`disable`・`set-login [--username] [--generate | --password-stdin]`・`hash-password`。サービスが動いていれば admin のトークンで `PUT /web`・`PUT /web/credentials` を送り（`elevate_main.AdminApi`。接続先の確認つき）、止まっていれば同じ検査（`web_changes`・`credential_changes`）で設定ファイルを直接変える。パスワードは引数で受け取らない。`config show` はハッシュを「（設定済み）」と表示し、`config set` では WebUI の項目を変えさせない（web のコマンドを案内する）
- ログイン・ログアウト・失敗・ロック・再確認・ON/OFF をログに残す（接続元付き）。通知の既読のログに経路（WebUI／トレイGUI・管理用コマンド）を加えた（6.10節）
- 二段階認証のコード（`code`）はログイン・再確認の本文で受け取れるが、12-5d まで使わない。全体のロック（LAN だけが対象）は 12-5e

## 74. 段階 12-5b: WebUI の画面

サービス化・Web API設計書 7.3節・11章。clamav-webui（利用者の別リポジトリ）の仕組みを流用し、配色はトレイGUI（`gui/theme.py`）にそろえた（12-5 の計画の決めたこと 3）。

- **ファイル**: `web/index.html`（1 枚の HTML。ログインと 10 画面を JavaScript で切り替える）、`web/app.js`、`web/style.css`。ビルドの工程は無い。配布物には spec の `_DATAS` で `web/` を入れる
- **配る部分**（`api/web_static.py`。WebUI の待ち受けにだけ付ける）: `/` と `/web` は `/web/` へ転送、`/web/`・`/web/login` は `index.html`、`/web/app.js`・`/web/style.css` だけを配る（パスから任意のファイルを読まない）。WebUI の応答にはすべて `nosniff`・`X-Frame-Options: DENY`・`Referrer-Policy: same-origin`・`Cache-Control: no-store`、HTML には CSP（`script-src 'self'; style-src 'self'` など。clamav-webui より厳しく、インラインのスタイルも使わない）
- **流用した仕組み**: fetch の包み（`api()`。エラーの JSON を `ApiError` にする）、`data-action` → `ACTIONS`（フォームは `FORMS`）、表示は `textContent` だけ（`el()` で要素を作る）。clamav-webui のダブルサブミットの CSRF（`csrf_token` Cookie）は使わず、ログイン応答の CSRF トークンをメモリに持つ。画面を読み直したときは `GET /auth/session`（Cookie のセッションが有効なら、そのセッションの CSRF トークンを返す。新設）
- **画面**: ホーム（状態のカード、クイックスキャン・定義更新・リアルタイム保護・clamd の起動停止）、スキャン（種類・場所、進み具合は SSE、見つかったものの「隔離」、履歴と詳細）、隔離（復元・削除）、除外、リアルタイム保護（開始・停止、検知したときの動作、監視するフォルダ）、スケジュール（追加・編集・有効無効・今すぐ実行・削除、「スリープ中でも起こして実行する」）、定義更新（ログは SSE）、ログ（絞り込み・CSV・消去）、設定（変えられる項目だけ。保護された項目は一覧とコマンドの案内）、診断
- **admin の操作**: E8108 を受けたら、操作の内容を並べた確認のダイアログ（`<dialog>`）でパスワードを求め、`POST /auth/reauth` の後に 1 回だけやり直す（トレイGUI の UAC と確認画面に相当）。WebUI はトレイGUI と違い、変更をためずにその場で送る（確認から 5 分間は確認を省くため、続けての操作で何度も聞かない）
- **通知**: 画面上部の「通知」に未読の件数。一覧から 1 件ずつ・すべて既読にできる。`notification`・`notification.acked` で一覧と件数をすぐ直す。新しい通知はページの右下にも出す。ブラウザの OS 通知は HTTPS とあわせて 12-5e
- **イベント**: `EventSource`（Cookie で認証）。切れて閉じた（ログインの期限切れ・WebUI の停止）ときは、3 秒後にセッションを確かめ、有効ならつなぎ直し、無効ならログインの画面に戻す
- `GET /status` の `web` に、WebUI の ON/OFF と OFF にする時刻を入れた（画面上部の「この WebUI は 18:30 に OFF になります」）
- **テスト**（`tests/unit/test_webui_pages.py`）: 応答のヘッダ、配るファイルの限定、内部 API では配らないこと、`innerHTML` などを使わないこと、インラインのスクリプト・ハンドラ・スタイルが無いこと、`data-action`・フォーム・画面とその処理の対応、`app.js` が呼ぶ API のパスが実在すること、扱うイベントをサービスが出していること、`node --check`（node があるとき）

### 74.1 実機の確認（Windows・Linux）を受けて直したこと

- **画面から始めたスキャンの検知も通知にする**: 通知を作っていたのはリアルタイム監視とスケジュールのスキャンだけで、トレイGUI・WebUI・API から始めたスキャンの検知では作らなかった。画面を閉じていても気づけるよう、`core/runtime.py` の `connect_scan_notifications` で `ScanDispatcher.file_scanned` の検知を `NotificationService.notify_detection`（`quarantined=False`。画面からのスキャンは自動では隔離しない）に渡す
- **WebUI の「隔離」に再確認を求める**: `POST /quarantine` は user の操作で、サービスでは頼んだ利用者のフォルダの中だけに限っていたが、WebUI のセッションは admin として扱うため、どこのファイルでも再確認なしで隔離できた。WebUI の待ち受けでは隔離ファイルの削除・復元と同じく、再確認から 5 分以内に限る（トレイGUI・内部 API の扱いは変えない）
- **セッションの Cookie の名前**を `session` から `clamqtpie_session` にした。Cookie はポートで分かれないため、このPC で動くほかのアプリ（127.0.0.1 の別のポート）が同じ名前の Cookie を使うと、ログインが上書きされて切れる
- **WebUI が OFF になった・サービスが止まったとき**: 画面が気づかず、操作するとブラウザの英語のエラーだけが出ていた。`fetch` の失敗を「サービスにつながりません。WebUI が OFF になったか、サービスが止まった可能性があります」にしてログインの画面に戻す。`EventSource` はつなぎ直しを続けて閉じないため、エラーのたびに（重ねずに）3 秒後にセッションを確かめる
- **変更の一覧はいつも見せる**: 設定の保存は、再確認から 5 分以内だと確認のダイアログが出ず、変更の一覧も見ずに保存されていた。`GET /auth/session` に `reauthenticated` を加え、5 分以内なら `confirm` で一覧を見せる（トレイGUI は毎回、一覧を見せる）
- **ログの CSV に「コード」の列**（E8101 など）を加えた（トレイGUI の CSV 出力も同じ）
- **画面**: スキャン履歴の結果を日本語に（完了・中断・失敗）、再確認のダイアログのパスワードの欄に見出し、変更の一覧の見出しの行に「・」を付けない、中立の知らせは灰色の線（赤はエラー・検知、緑は成功だけ）、長い画面をスクロールしてもメニューは見えたまま、幅が狭いときの上部の折り返し、パスの入力欄を広く、除外の入力欄の例を OS によらない書き方に、診断で [注意] があれば上に件数を書き、「伏せ字のレポートをコピー」を加えた
- **`clamqtpie-ctl web`**: `enable` のエラー（パスワードが未設定）に `web set-login` を案内、`status` は OFF のとき URL を出さない、`config show` はパスワードの文字数（`web_password_length`）も伏せる、`--password-stdin` は端末で手入力するときは入力した文字を出さない（`getpass`）

## 75. 段階 12-5c: トレイの「WebUI」サブメニュー

サービス化・Web API設計書 3.3節・10章。12-5 の計画（internal の `docs/12-5計画.md`）の 12-5c。

- **メニュー**（`gui/webui_menu.py`。トレイのメニューの「WebUI」。サービスにつながってから出す）: 状態の行（「WebUI: ON（21:54 に OFF）」など。押せない）、ブラウザで開く（待ち受けているときだけ）、ON にする（1 時間・8 時間・24 時間・時間を指定...・OFF にするまで。ON の間は押せず、「OFF にする時刻を変更...」を使う）、OFF にする時刻を変更...、OFF にする、ログイン設定...。状態は `GET /web`（user）で、メニューを開くたびと操作の後に読み直す
- **昇格**: `PUT /web`・`PUT /web/credentials` は Bearer の admin だけなので、管理者の操作の一覧（`api_client/admin_ops.py`）に加え、昇格ヘルパーを通す。確認画面の説明は、ON（いつ自動で OFF にするか）・OFF（ログイン中の WebUI はすべてログアウト）・時刻の変更・ログインの設定（ユーザー名だけ。パスワードは出さない）
- **ON にするときにパスワードが未設定**なら、先にログイン設定のダイアログを開き、ログインの設定と ON を 1 回の昇格にまとめる
- **ダイアログ**（`gui/dialogs/webui_dialogs.py`）: OFF にする時刻（長さ `90m`・`8h`・`2d` か日時）、ログイン設定（ユーザー名・パスワード 2 回、または自動で作る 20 文字）、自動で作ったパスワードを 1 回だけ見せる（コピーできる）。入力の検査はサービスと同じ関数（`core/duration_text.py`・`core/web_password.py`）
- LAN・二段階認証・HTTPS の項目は 12-5d・12-5e で足す

### 75.1 12-5b の実機の確認で出た小さな直し

- `clamqtpie-ctl web enable|timer --until` に時刻だけ（`23:00`）を書けるようにした（今日のその時刻、過ぎていれば明日）
- 監視するフォルダが無いときにリアルタイム保護を始めようとしたときの文を「監視するフォルダがありません。先に監視するフォルダを追加してください」にした（新しく導入した直後はこの状態から始まる）
- 診断の伏せ字の版（`POST /diagnostics`）の先頭に、伏せたもの（ホーム・ユーザー名・コンピューター名、アプリのフォルダ、ソケット・clamd のホスト、設定ファイルと clamonacc の場所）と伏せていないもの（ClamAV の実行ファイル・定義のフォルダ）の説明を添えた（`core/diagnostics_service.py` の `MASK_NOTE`）。伏せる基準は以前のレビューで決めたとおり変えない

### 75.2 12-5c の実機の確認で出た直し

- サービスが `ValueError` で断ったとき（入力の誤り・「監視するフォルダがありません…」など）は、その文を応答の `message` にし、`detail` には入れないようにした（`api/errors.py`）。トレイGUI のエラーのダイアログはメッセージとコードだけを出すため、昇格ヘルパーを通した操作では「リクエストの内容が正しくありません [E8100]」だけになり、案内の文が届いていなかった。WebUI でも「リクエストの内容が正しくありません（…）」の前置きが無くなる。断った要求はログにも E8100 で残す（「詳細を見る」で探せるように）。FastAPI の入力の検証（`RequestValidationError`）は、これまでどおり固定の文と項目名だけを返す
- リアルタイム監視を始めるときに監視するフォルダが無ければ、開始を送る前に案内するようにした（トレイの「リアルタイム監視 ON/OFF」とリアルタイム保護の画面）。サービスに送ると、pkexec・UAC の認証と昇格ヘルパーの確認画面を通った後で断られていた
- 「WebUI のログイン設定」「WebUI を OFF にする時刻」「WebUI のパスワード」のボタンを「キャンセル」「閉じる」にした（Qt の翻訳は配布物から外しているため、英語のままだった）。「長さで決める」の入力欄の例を短くし、書き方は欄の下に添えた
- 暗い色の画面で、印の付いていないチェックボックス・ラジオボタンの枠が見えにくい点は、アプリ全体の配色（`gui/theme.py`）の問題のため、ここでは直していない

## 76. 段階 12-5d: 二段階認証（TOTP）

サービス化・Web API設計書 3.3節・6.11節。12-5 の計画（internal の `docs/12-5計画.md`）の 12-5d。

- **計算**（`core/totp.py`）: RFC 6238（HMAC-SHA1、30 秒、6 桁）を標準ライブラリで計算する。前後 1 ステップまで受け付け、一度受け付けたステップ番号より前・同じものは受け付けない（`api.web_totp_last_step`。再起動しても引き継ぐ）。秘密鍵は 20 バイトの乱数（Base32、32 文字）。予備のコードは 10 個（英数字 10 文字、紛らわしい文字を除く。表示は `ABCDE-FGHJK`）で、scrypt のハッシュだけを `api.web_recovery_code_hashes` に保存し、使ったものは消す。QR コードは `segno`（依存に追加。pure Python）
- **秘密鍵の置き場所**: SMTP パスワードと同じ秘密の保存先（`ConfigService.get/set/delete_totp_secret`、名前 `web_totp_secret`）。`config.json` には入れない。設定済みなのに秘密鍵を読めないときは、二段階認証を外さずにログインを断り、ログに `web totp reset` を案内する。`api.web_totp_enabled` が壊れていても設定済みとして扱う（設定ファイルの破損で二段階認証が外れないように）
- **ログイン**: 設定済みなら、パスワードに加えて確認コード（予備のコードも可）が要る。パスワードが違っても確認コードの照合を行い（どちらが違ったかを時間で見分けさせない）、使ったことにするのは両方が合ったときだけ。失敗の文は「ユーザー名・パスワード・確認コードのどれかが違います」。失敗はパスワードと同じく総当たり対策の回数に数える。ログインの画面は、二段階認証を設定しているかをログインの前に知らせないよう、確認コードの欄をいつも出す（設定していなければ空欄でよい）
- **再確認**（`POST /auth/reauth`）: 設定済みなら確認コードで確かめ、パスワードは使わない（設計書どおり）。`POST /auth/session`・`GET /auth/session` の応答の `totp` で、画面はどちらを聞くかを決める
- **設計書からの変更: 設定の API を `POST /web/totp {"secret", "code"}` の 1 つにした**（設計書は `POST /web/totp/setup` で秘密鍵を作り、`/confirm` で確かめる 2 つ）。どちらも admin の Bearer だけなので、トレイGUI では QR コードの前後で UAC・pkexec と確認画面が 2 回出てしまう。秘密鍵は頼む側（トレイGUI・`clamqtpie-ctl`）が作って QR コードを見せ、利用者が認証アプリのコードを入れた後で、秘密鍵とコードを一緒に送る（昇格は 1 回。サービスは仮の秘密鍵を持たなくて済む）。コードが合えば有効にし、セッションをすべて破棄して、予備のコードをこの応答でだけ返す。解除は `DELETE /web/totp`（秘密鍵・予備のコードを消し、セッションを破棄）
- **トレイ**: 「WebUI」→「二段階認証の設定...」（設定済みなら「（設定済み）」と付け、「設定し直す」「解除する」を選ぶ）。QR コード・手で入れるときの秘密鍵・コードの欄のダイアログの後、1 回の昇格で送り、予備のコードを 1 回だけ見せる（コピーできる）。昇格ヘルパーの確認画面には、秘密鍵もコードも出さない
- **clamqtpie-ctl**: `web totp setup | status | reset [--yes]`。setup は端末に文字で QR コード（白地に黒。表示できない文字コードの端末では秘密鍵だけ）と秘密鍵を表示し、コードを入れてもらってから送る（サービスが止まっていれば設定を直接変える）。予備のコードは端末にだけ 1 回表示する。`config show` は予備のコードのハッシュを個数だけにし、`config set` では二段階認証の項目を変えさせない
- LAN からの接続を許可するときに二段階認証を必須にする確かめと、解除したときに LAN 許可を OFF にするのは、LAN からの接続（12-5e）とあわせて作る

設計の見直し（実機の確認の前に行った。設計書にない点を足した。設計書の本体は 12-6 で直す）:

- **予備のコードを使ったときの知らせ**: ログイン・再確認で予備のコードを使ったら、応答に `recovery_used`・`recovery_codes_left` を付け、画面は残りの数と、設定し直す方法（トレイの「二段階認証の設定...」か `web totp setup`。WebUI からは設定し直せない）を知らせる（見落とさないよう alert）。0 個になったときは「予備のコードがなくなりました」と強めの文にする。常に出す注意・OS の通知は出さない（減るのは使ったときだけのため）。予備のコードだけを作り直す手段は設けない（新しい予備のコードは、設定し直して得る）
- **確認コードの間違いが続いたらログインを止める**: パスワードは合っていて確認コードだけが違ったログインを `api.web_totp_failures` に数え、ログに「パスワードが漏れているおそれがあります」と残す。10 回（`totp.STOP_FAILURES`）になったら、接続元を問わず（ループバックも）パスワードと確認コードでのログインを止め、トレイに通知してパスワードの変更を勧める。止めている間も、パスワードと予備のコードでのログインは受け付ける。回数を 0 に戻して停止を解くのは管理者の操作だけ（`POST /web/unlock`（admin の Bearer だけ）、トレイの「WebUI」→「ログインの停止を解く」（止めているときだけ出る）、`clamqtpie-ctl web unlock`、パスワードの変更）。時間・ログインの成功・サービスの再起動では戻さない（パスワードが漏れた後に、確認コードを時間をかけて当てられないように）。`GET /web` の `login_stopped`・`web status` に出す。壊れた値は止めた側に倒す
- **止めている間の応答**: パスワード・確認コードの正否によらず、接続元ごとのロックと同じ 429（E8106）と同じ文にする（止まったことから、パスワードが合っていたと見分けさせない）。そのため接続元ごとのロックの応答からも、残りの分（「約 N 分後」）を外した
- **認証アプリに登録する名前**: ユーザー名ではなく、この PC の名前（`ClamQtPie:<PC の名前>`。Windows は `COMPUTERNAME`、Linux はホスト名）。複数の PC の WebUI を認証アプリで見分けられ、ユーザー名を変えてもずれない。秘密鍵を作るトレイGUI・`clamqtpie-ctl` がその場で取る
- **トレイでの設定で確認コードが切れたとき**（Linux 実機の確認で見つけた）: 確認コードは昇格の確認（UAC・pkexec と確認画面）の後でサービスが確かめるので、確認画面で時間をかけると前後 1 ステップを外れて断られる（E6002、detail `code`）。そのときは同じ秘密鍵のまま設定のダイアログを開き直し、断られた理由を出して新しいコードを入れてもらう（秘密鍵を作り直すと、認証アプリに登録し直すことになるため）。受け付ける幅は広げない

## 77. サービスの起動の回数の上限（Linux）の扱い

サービス化・Web API設計書 9.4節の回復動作。12-5d の Linux 実機の確認で、導入 2 回と確認用スクリプトの再起動 2 回で上限に当たり、確認が途中で止まったため見直した（上限の値は変えない）。

- **前提の違い**: Windows の回復動作（`sc failure`）は落ちたときの自動の再起動にだけ効き、手での開始・導入は数えない。Linux（systemd）の `StartLimitBurst=3`・`StartLimitIntervalSec=1d` は、手での `systemctl start/restart`・導入の `enable --now` を含むすべての開始を数え、超えると手での開始も断る（`start-limit-hit`）。回数は `systemctl reset-failed` か PC の再起動で 0 に戻る
- **導入・更新では数え直す**: `install.sh` は `systemctl enable --now` の前に `systemctl reset-failed clamqtpie.service` を実行する（ctl の `--restart` と同じ扱い）。導入は管理者が意図して行う操作で、上限の対象（落ち続けるサービス）ではないため。導入し直すと回数は 0 に戻り、導入そのものの開始が 1 回目になる
- **上限で止まっているときの知らせ**: `clamqtpie-ctl status` は、systemd の状態が failed・理由が `start-limit-hit`（`core/service_status.service_stop_reason`）なら、上限で止めていること、原因を確かめる場所（`journalctl -u clamqtpie`・`/var/log/clamqtpie/`）、直したあとに実行する `sudo systemctl reset-failed clamqtpie && sudo systemctl start clamqtpie` を表示する。トレイGUI の「サービスにつながりません」には `clamqtpie-ctl status` の案内を添える（ctl は root で動かすので、Linux は `sudo clamqtpie-ctl status`、Windows は管理者のコマンドプロンプトで、と書く）。数え直して開始するコマンドは作らない（原因を確かめずに起動し直す流れを作らない）。区別は Linux だけ（Windows は回復動作の後も手で開始できる）
- **確認・デバッグ中**: その PC にだけ drop-in（`/etc/systemd/system/clamqtpie.service.d/debug-no-start-limit.conf`、`[Unit] StartLimitIntervalSec=0`）を置いて上限を外し、終わったら消す。置く・消す・状態を確かめるスクリプトは internal の `tools/` にあり、配布物・ctl には入れない
- README の困ったときの節への記載は、12-6 の README の書き直しで行う

## 78. 配布物: Python のモジュールを実行ファイルどうしで共有する

段階 12 で実行ファイルが 4 つ（トレイGUI・サービス・昇格ヘルパー・clamqtpie-ctl。Windows は通知のクリックの ClamQtPieActivate も）になり、PyInstaller の既定ではそれぞれが自分の Python のアーカイブ（PYZ）に同じモジュールを持っていた。

- **noarchive**: `ClamQtPie.spec` の Analysis を `noarchive=True` にし、モジュールを `_internal/` に .pyc として置く。実行ファイルは同じ `_internal/` を使うので、同じモジュールが 1 回だけ入る。ファイルの数は増える（Windows で約 830 → 約 2130）が、合計の大きさと起動の時間は減る
- **入れないもの**: 実行時に使わない `pygments`（mako のエラー表示の色付け・httpx のコマンドから。mako は無ければ色付けなしに切り替える）・`setuptools`・`pkg_resources`・`_distutils_hack`・`pydoc_data` を `_EXCLUDES` に足した
- **MERGE を使わなかった理由**: PyInstaller の `MERGE` は、実行ファイルどうしで共通の依存（DLL・データ）を 1 か所にまとめる仕組みで、PYZ の中身は対象にしない。また、まとめられた側の実行ファイルは onefile と同じ扱い（展開して使う）になり、onedir の配布物には合わない
- **測った値**（変更前 → 変更後。Linux は WSL（Ubuntu）でビルドした値。大きさ・起動の時間はビルドした環境の Python・Qt のライブラリで変わり、Arch 系の実機でビルドした変更後の配布物は tar.gz 89.1 MB・展開後 237 MB・1572 ファイル・起動 約 150 ms だった。比べるときは同じ環境の変更前と比べる）:

| | 変更前 | 変更後 |
|---|---|---|
| Windows のインストーラ | 70.4 MB | 31.3 MB |
| Windows の導入後 | 130 MB・833 ファイル | 113 MB・2134 ファイル |
| Windows の `clamqtpie-ctl --help` の起動 | 764 ms | 157 ms |
| Linux の tar.gz | 109.3 MB | 73.3 MB |
| Linux の展開後 | 217 MB・247 ファイル | 199 MB・1513 ファイル |
| Linux の `clamqtpie-ctl --help` の起動 | 161 ms | 100 ms |

- テスト（`test_service_access.py`）: spec の Analysis が `noarchive=True` で、実行時に使わないものを入れないことを確かめる

## 79. 段階 12-5e1: WebUI の HTTPS（ローカル CA）

サービス化・Web API設計書 3.5節。12-5e は 2 つに分けた（e1: HTTPS とローカル CA、e2: LAN からの接続）。決めたことの記録は internal の `docs/dig/2026-11-05-12-5e.md`。HTTPS は LAN からの接続の前提で、v0.10.0 では `clamqtpie-ctl web tls` だけで有効にする（トレイGUI には出さない）。

- **LAN の範囲**（`core/net_scope.py`）: 各インターフェースの IPv4 のアドレスとネットマスク（psutil）から、直接つながっているサブネットを求める。プライベートアドレス（10/8・172.16/12・192.168/16）だけで、リンクローカル・ループバック・/32 を除く。仮想ブリッジ・VPN（`docker*`・`br-*`・`virbr*`・`veth*`・`tun*`・`wg*`・`vEthernet (WSL…)`・`vEthernet (Default Switch)` など）を名前で除く。Windows のインターフェースの名前は「イーサネット 3」のような表示名で見分けられないため、レジストリのアダプターの説明（DriverDesc。`VirtualBox Host-Only`・`TAP-Windows`・`WireGuard` など）でも除く（実機で VirtualBox のホストだけのネットワーク 192.168.56.0/24 が LAN に入っていたため）。e2 の接続元の検査・待ち受けのアドレスもこれを使う
- **IPv4 だけ**（設計書からの変更）: 家庭の IPv6 の多くはグローバルアドレスで許可の条件（プライベートだけ）に入らず、一時アドレスは 1 日ほどで変わって証明書の作り直しが続くため。ループバックの `::1` は今までどおり
- **ローカル CA**（`core/local_ca.py`）: ECDSA P-256・2 年・`BasicConstraints(ca, pathlen=0)`・Name Constraints（critical）。許す名前は localhost・このPC の名前（短い名前・FQDN・`.local`・`.lan`・`.home.arpa`。DNS に使えない文字を含む名前は入れない）、許す IP は 127.0.0.0/8 と CA を作った時点の LAN のサブネット。サーバー証明書は 1 年・SAN は名前と 127.0.0.1・各 LAN のアドレス（CA の Name Constraints の外のものは入れない）。置き場所は `<データ>/tls/`（`ServicePaths.tls_dir`。鍵は `write_private_file` で SYSTEM・root だけ）
- **自動更新**: WebUI を HTTPS で待ち受けるときと 1 日 1 回（`WebController.check_tls`。2 秒ごとの定期処理から呼び、前回から 1 日たったときだけ確かめる）、サーバー証明書の名前・アドレスの変化か期限まで 30 日を切ったことを見て作り直し、待ち受けだけを始め直す（セッションは残す）
- **CA は自動では作り直さない**: 作り直すとすべての端末で登録のやり直しになるため。期限の 30 日前から・サブネットが CA を作ったときと変わったら、`GET /web` の `tls_info.notes`・`web status`・`web tls status` で `web tls reset-ca` を案内する。期限が切れたら作らずに、前のサーバー証明書があれば HTTPS のまま待ち受ける（ブラウザに警告が出る。LAN 許可を OFF にするのは e2）
- **このPC の信頼ストアには登録しない**: ループバックは HTTP でもブラウザが安全な接続として扱うので、このPC で HTTPS を使う利点は小さく、信頼ストアの書き換えは乗っ取られたときの影響を広げる。このPC もほかの端末と同じく `web tls export-ca` で書き出して手で登録する
- **CA 証明書の配布**（設計書からの変更）: HTTP の別ポート（`api.web_ca_port`、8767）は作らない。HTTPS の WebUI がログインの前でも `/web/ca.crt`（DER。Android・Windows・macOS でそのまま開ける）と `/web/ca-info`（SHA-256 のフィンガープリント）を配り、ログイン画面に「CA 証明書をダウンロード」とフィンガープリントを出す。守りの中心はフィンガープリントの照合で、別ポートで得られるのは「警告を一度越えなくてよい」ことだけのため。HTTP のときは 404。`http://` で開いたときの HTTPS への転送は無い
- **待ち受け**: `ApiServer(tls=(サーバー証明書, 鍵))` で uvicorn に渡す（TLS 1.2 より古いものは Python の既定で受け付けない）。HTTPS の方式を変えたら待ち受けを始め直し、セッションをすべて破棄する
- **Cookie**: HTTPS のときは `__Host-clamqtpie_session`（Secure・Path=/・Domain なし）。HTTP のときは今までどおり `clamqtpie_session`。どちらかは要求の scheme（`proxy_headers` は無効なので実際の接続のもの）で決める。HSTS は付けない。状態を変える要求の Origin は、scheme も含めて Host と同じであること（HTTPS の WebUI に HTTP のページから送らせない）
- **CA を作り直した後の古いタブ**（Linux 実機の確認で見つけた）: 作り直す前から開いていたタブは、証明書の検証が通らずに要求が失敗するが、ブラウザの JavaScript には理由（証明書のエラーか）が渡らず、「サービスにつながりません」と見分けられない。そのため、つながらないときの文に「HTTPS の証明書（ローカル CA）を作り直した場合は、ページを読み直してください」を足した
- **API**: `PUT /web/tls {"mode": "off"|"local_ca"}`・`POST /web/tls/reset-ca`（admin の Bearer だけ。reset-ca は前の CA のフィンガープリントを返す）。`GET /web` の `tls`・`tls_info`
- **clamqtpie-ctl**: `web tls on|off|status|export-ca [FILE]|reset-ca [--yes]`。サービスが止まっていれば設定ファイルを変える・CA を消す（CA を作るのはサービスだけ）。reset-ca は、ローカル CA は失効させられないので前の CA を各端末から消すよう、フィンガープリント付きで案内する。CA の鍵を書き出すコマンドは無い。`web tls on|off` は、WebUI が ON なら切り替えた後の URL も出す（scheme が変わるため。Windows 実機の確認で、前の `http://` の URL を開いてしまったことから）
- **Windows で CA 証明書を登録するとき**（Windows 実機の確認で見つけた）: .crt を開いて証明書のインポート ウィザードで「証明書の種類に基づいて、自動的に証明書ストアを選択する」を選ぶと、CA が「中間証明機関」に入る。Firefox 系のブラウザ（Floorp で確認）は Windows の証明書も読み込む（`security.enterprise_roots.enabled`）ため、ブラウザ自身に CA を登録して信頼しても、同じ CA が Windows の中間証明機関にあると `SEC_ERROR_UNKNOWN_ISSUER` で警告が消えない（中間証明機関から消すと通る。同じ作りのテスト用の証明書で再現と解消を確かめた）。README の登録手順（12-6）では、Windows は「証明書をすべて次のストアに配置する」→「信頼されたルート証明機関」を選ぶこと、Firefox 系は中間証明機関に入っていたら消すことを書く
- **設定**: `api.web_tls`（`"off"`・`"local_ca"`、既定 off）。利用者が用意した証明書（`"custom"`・`use-cert`）は v0.10.0 では受け付けない（不正な値として off に戻す）
- 依存に `cryptography` を加えた（証明書の作成。標準ライブラリの `ssl` では作れない）
- **配布物の python3t.dll**（ビルドで見つけた）: Windows の Python 3.15 用の cryptography（abi3・abi3t の両方の印の wheel）の `_rust.pyd` は `python3t.dll` を読み込む。開発環境では Python 本体の `abi3t-compat/python3t.dll` が読み込まれるが、PyInstaller はこれを集めないため、配布物では「DLL load failed while importing _rust」になった。spec の `_extra_binaries()` で `_internal/` に入れる
- セキュリティの確かめに bandit を使う（開発用。Windows の `.venv` は Python 3.15 で PyYAML のビルド済みの版が無いため、WSL の venv に入れた）。12-5e1 で変えたファイルに新しい指摘はなかった
## 80. 導入した直後のサインインし直し（Windows）

Windows の実機の確認（#46）で見つけた。グループのメンバーシップはサインインしたときにトークンに入るため、インストーラが利用者を ClamQtPie Users に新しく入れても、サインインし直すまで効かない。インストーラが最後に元の利用者の権限で起動するトレイGUI は user.token を読めず、E8110 で終わっていた（それまでの確認は管理者の PowerShell からインストーラを動かしていて、トレイGUI も昇格して起動し、Administrators の権限で読めていたため表に出なかった）。グループを消して作り直したときも、SID が変わるので同じことが起きる。

- **clamqtpie-ctl setup install**: 利用者をグループに新しく入れたら、終了コード 10（`EXIT_SIGNIN_REQUIRED`）で返す（`setup_install` が True を返す）。すでに入っていれば今までどおり 0
- **インストーラ**: 10 なら完了の画面で再起動の確認を出す（`NeedRestart`。文は `[Messages]` の `FinishedRestartLabel` で、サインアウトしてサインインし直すまでトレイGUI は使えないことと、その代わりに今すぐ再起動するかを尋ねる）。トレイGUI は起動しない（`[Run]` の `Check: not SignInRequired`）。次のサインインで自動起動から起動する。更新（すでに入っている）は今までどおり
- **トレイGUI の E8110**: グループに入っているのに、今のサインインのトークン（Linux は補助グループ）に入っていなければ（`service_access.signin_is_stale`）、追加の依頼ではなく、サインアウトしてサインインし直す（だめなら再起動）よう案内する。Windows は `CheckTokenMembership`、Linux は `os.getgroups()` で確かめる。確かめられなければ今までの案内
- Linux の install.sh は、以前からグループに入れたときに「サインアウトしてサインインし直す」と表示している
## 81. サービスを通常の自動起動にする（Windows）

段階 12-4d からサービスは遅延自動起動（`--startup delayed`）だったが、PC を起動してサインインするたびに、トレイGUI がサービスの起動（自動起動の約 2 分後）までつながらず、「サービスにつながりません（E8105）」の警告が出ていた（Windows の実機で確認）。遅延にしなければならない理由は無く、ウイルス対策のサービスは起動の直後から動いているほうが役割に合う（リアルタイム監視・スケジュールが早く始まり、サービスが止まっている間に別のプロセスがポートで待ち受けられる時間も短くなる）ため、通常の自動起動（`--startup auto`）にした。

- `register_service` は導入でも更新でも `--startup auto` で登録する（pywin32 は `auto` のとき遅延の設定を外すので、遅延で導入済みのサービスも更新で通常の自動起動になる）
- clamd がまだ起動していなくても、サービスは今までどおりつなぎ直す
## 82. 段階 12-5e2: WebUI の LAN からの接続（試験的）

サービス化・Web API設計書 3.1節・3.3節。決めたことの記録は internal の `docs/dig/2026-11-05-12-5e.md`。v0.10.0 では試験的で、有効にできるのは `clamqtpie-ctl web lan on` だけ（トレイGUI には設定の項目を出さない）。

- **許可の条件**（`core/web_lan.lan_problems`）: HTTPS（ローカル CA）・二段階認証を設定済み・パスワードが 12 文字以上（`web_password.MIN_LENGTH_LAN`）。満たさなければ `PUT /web {"lan": true}` は 400。HTTPS の無効化（`tls_changes`）・二段階認証の解除（`disable_totp`。ctl の `web totp reset` も）・12 文字より短いパスワード（`credential_changes`）・CA の期限切れ（`_prepare_tls`・`check_tls`。通知する）で `api.web_lan` を false に戻す。設定ファイルを書き換えて条件を満たさない true にしても、読み込むときに false に直す
- **WebUI を OFF にしても許可は残す**（dig Q9）。ON にすると LAN からも待ち受ける。LAN から待ち受けていることは隠さず見せる: `web status`、トレイの WebUI のサブメニュー（「LAN からの接続: 許可（試験的）」の行）、トレイから ON にするときの昇格の確認画面（本文に `lan: true` を付けて送り、「LAN からも接続できます」と出す）、WebUI の画面の上の帯
- **待ち受け**: HTTPS で待ち受けるときだけ、ループバックと LAN の各アドレス（`LanScope.listen_hosts`）の同じポートで待ち受ける（`ApiServer(hosts=...)`。uvicorn にソケットを複数渡す）。`create_listen_socket` は、ループバックのほかは `allow_lan` のときの LAN のアドレス（プライベートの IPv4）だけを受け付ける（`0.0.0.0` などで待ち受けない）。LAN のアドレス・サブネットの変化は 1 分ごとに確かめ（`check_lan`）、変わったら待ち受けを始め直す（サーバー証明書も、名前・アドレスが変われば作り直される）
- **接続元・Host**（`api/auth._check_web_request`）: LAN から待ち受けているときだけ（`web.scope`）、接続元に LAN のサブネットを、Host にこのPC の名前（短い名前・`.local`・`.lan`・`.home.arpa`・FQDN）と LAN のアドレスを加える。Origin が Host と同じであることは今までどおり
- **セッション**: LAN からの接続を許可している間の有効期限は 15 分（設定の値がもっと短ければそちら。`web_lan.session_timeout_min`）。許可を変えたらセッションをすべて破棄する
- **全体のロック**（`LoginGuard.lan_failure`）: LAN からのログイン・再確認の失敗が 1 時間に 30 回になったら、LAN からのログインを 15 分止めて通知する（接続元を変えながらの総当たり）。このPC のブラウザからのログインは止めない。接続元ごとのロック（5 回で 5 分）は今までどおり
- **Windows のファイアウォール**: LAN から待ち受けるときに受信の規則 `ClamQtPie WebUI`（TCP の WebUI のポート、`remoteip=localsubnet`、`profile=private`、サービスの実行ファイルだけ）を作り直し、待ち受けをやめたら消す。削除（`setup uninstall`）でも消す。LAN のネットワークがパブリックなら、規則は広げずに「プライベートに切り替えるとつながる（信頼できないネットワークでは切り替えない）」と知らせる（`Get-NetConnectionProfile`。dig Q8）
- **Linux のファイアウォール**: 変えない。ufw・firewalld が有効なら開け方を、無効なら注意を知らせる（`web status` の「注意」）
- **トレイGUI の LAN・HTTPS の設定の項目と、隠し設定 `api.web_lan_menu` は作らなかった**（全体の dig で、v0.10.0 では隠す前提だった）。隠したまま使われない UI を足すより、試験的でなくすときにトレイの項目とあわせて作るほうが、確かめる範囲が小さいため
- `clamqtpie-ctl web lan on|off`。HTTPS の無効化・二段階認証の解除・短いパスワードで許可が OFF になったら、そのことも表示する。`api.web_lan` は `config set` では変えさせない
- **`clamqtpie-ctl web tls renew`**（`POST /web/tls/renew`）: CA はそのままで、サーバー証明書だけを作り直す。Windows の実機の確認（Android の Firefox）で見つけた: 端末で警告を越えて証明書の例外を作ると、後で CA を登録しても、例外がある間は「接続が安全ではありません」と表示され続け、Android の Firefox には例外を消す画面が無い。例外はそのときのサーバー証明書に結び付くため、サーバー証明書を作り直すと使われなくなり、CA で確かめられる（実機で確かめた）。CA の登録はやり直さなくてよい
- **`clamqtpie-ctl config set --restart` の止まるのを待つ処理**（Windows 実機の確認で見つけた不具合）: `sc stop` の後、`sc query` に RUNNING が無いことで止まったとみなしていたため、止まる途中（STOP_PENDING）で設定を書き換えて開始し、開始が 1056（動いている）で失敗していた（止まる途中のサービスが設定を上書きするおそれもある）。STOPPED（か登録が無い）になるまで待つ（`service_status.service_stopped`）
## 83. OS の定義更新サービス（clamav-freshclam）の開始・停止（Linux）

Linux の実機の確認で見つけた。clamav-freshclam が動いていると、ClamQtPie の自動更新は重ねて更新しないよう見送る（段階 12-3c）が、見送るたびに「実行しませんでした」をログに残していて、自動更新の間隔ごとに同じ行が並んでいた。また、ClamQtPie からは clamav-freshclam を止められず、ClamQtPie の自動更新に任せたくても端末で `systemctl` を使うしかなかった。

- **定義更新の画面**（トレイGUI の「セキュリティ インテリジェンス」・WebUI の「定義更新」）に「OS の定義更新サービス (clamav-freshclam)」の欄を足した。状態（動いています／止まっています）、PC の起動時に自動で起動する（する／しない）、誰が定義を更新しているか（「定義の更新は OS のサービスが行っています」／「ClamQtPie が自動更新します」）を出し、開始・停止のボタンを置く。clamav-freshclam が無い OS（Windows・未導入・systemd が無い）では出さない
- **停止は `systemctl disable --now`、開始は `systemctl enable --now`**（`core/freshclam_service.py`）。止めても次の起動でまた動くと、ClamQtPie の自動更新は見送ったままになるため、今の状態と PC の起動時の両方を揃える。片方だけ違う（端末で `stop` だけした等）ときは、どちらのボタンも押せる。状態は `systemctl show -p LoadState -p ActiveState -p UnitFileState` で読む
- **API**: `GET /updates/freshclam-service`（user）、`PUT /updates/freshclam-service {"enabled": bool}`（admin。トレイGUI は昇格の確認画面、WebUI は再確認）。サービスは root で動くため `systemctl` をそのまま呼ぶ（root でなければ pkexec）。失敗は E8004
- **見送りのログ**: 見送りが始まったときだけ記録し、OS のサービスが動いている間は記録しない。OS のサービスが止まったら覚えを消し、また動いて見送るときに記録する（サービスの起動ごとにも 1 回記録する）

## 84. コンソールのウィンドウが一瞬見える（Windows）

Windows の実機の確認（インストーラで入れたとき）で見つけた。ウィンドウを持たないプロセス（トレイGUI など）がコンソールのコマンドをそのまま起動すると、子プロセスごとに新しいコンソールが開き、一瞬見える。Windows 11 で既定の端末が Windows Terminal（「Windows に任せる」を含む）だと、Windows Terminal のウィンドウが開く（この PC で、ウィンドウを持たない Python から確かめた。`SW_HIDE` を付けて起動する Inno Setup の `Exec` と、`CREATE_NO_WINDOW` では開かず、指定なしでは開いた）。

- 指定が無かったのは、トレイGUI が通知（Toast）を出す・消す PowerShell、サインインし直しの判定の `net localgroup`、診断の `powercfg`、サービスの状態の `sc query`、`clamqtpie-ctl` の `setup`（`sc`・`icacls` など）とサービスの開始・停止、Windows の freshclam。インストールの直後にトレイGUI が起動して未読の通知を出すときなどに見えていた
- Windows でコンソールのコマンドを起動するところには、すべて `core/exec_path.no_window_kwargs`（`CREATE_NO_WINDOW`）を付ける（`clamd_process` にあったものを移した。ctl・サービスの状態の確認から PySide6 を読み込まないように）。新しく起動するところを足すときも付ける

## 85. 監視するフォルダの案内・呼び方・断った要求のログのカテゴリ（#43 の持ち越し）

段階 12-5c の Linux 実機の確認（#43）で挙がった気になった点のうち、v0.10.0 の前に直すと決めた 3 点。

- **WebUI でも先に案内する**: WebUI の「始める」は、パスワードの再確認の後でサービスに断られていた（E8100）。トレイGUI と同じく、有効な監視するフォルダが無ければ、再確認の前に「監視するフォルダがありません。先に、リアルタイム保護の画面の「追加」から…」と案内して送らない（`web/app.js` の `toggleRealtime`）。サービスの確かめ（`api/actions.py`）は残す
- **呼び方をそろえた**: 画面によって「監視対象」「監視対象フォルダー」「監視するフォルダ」が混ざり、案内の文は「「追加」から」と書いていたのにトレイGUI のボタンは「監視対象を追加」だった。利用者に見せる文（トレイGUI の画面・ボタン・案内、昇格ヘルパーの確認画面、ログ、ctl の migrate の表示、E2001 の文）は「監視するフォルダ」にした。トレイGUI のボタンは「フォルダを追加」「フォルダを外す」「有効/無効を切替」で、案内の文はそのボタンの名前を指す（WebUI のボタンは「追加」「外す」）。トレイGUI の「フォルダー」も「フォルダ」にした。コードのコメント・変数名は変えていない
- **断った要求のログのカテゴリ**: 断った要求（E8100・E8102）のログはカテゴリが付かず「システム」に入り、リアルタイムの操作を断ったものが「リアルタイム」の絞り込みで出なかった。API のパスの先頭で、`scans` はスキャン、`realtime` はリアルタイム、`updates` は更新、`quarantine` は隔離にし、ほかはシステムのまま（`api/errors.py` の `_category_for`）。スケジュールはスキャンと更新の両方があるため、システムのまま
- **E6001 の文**: 「実行ファイルの配置場所の書き込み権限を確認してください」はポータブル版のときの文で、設定をサービスがプログラムと別の場所（`%ProgramData%\ClamQtPie`・`/etc/clamqtpie`）に保存する今の形に合わないため、「設定ファイルの置き場所の空き容量と書き込み権限を確認してください」にした

#52 の Linux 実機の確認で挙がった気になった点のうち、この PR で直したもの。

- **外す予定の行の表示**: トレイGUI の監視するフォルダの一覧で、外す予定の行の「有効」の欄が「削除（保存前）」と出て、ボタン・確認画面の「外す」とそろっていなかった。「外す（保存前）」にした（`PendingEdits.state_text` の `removed_label`）。除外・スケジュールの一覧は「削除」のまま
- **［フォルダを外す］の表示**: 外す予定の行をもう一度［フォルダを外す］で選ぶと外すのをやめる動きは残し、外す予定の行を選んでいる間はボタンを［外すのをやめる］、保存前の追加の行なら［追加をやめる］と出して、押すと何が起きるかを示す（`RealtimeScreen._update_remove_target_button`）
- **同じフォルダの重複の登録**: 重複の確かめが文字列の完全一致だけで、`/tmp/` と `/tmp` のような書き方の違いでは同じフォルダを 2 回登録できた。`os.path.normpath`（Windows は `normcase` も）でそろえて比べ、`normpath` の形で保存する（`api/actions.py` の `add_watch_target`）。すでにある書き方の違う行は消さない

同じ確認で挙がった、WebUI での変更がトレイGUI の一覧に出ない件（サービスが変更の知らせを出していない）は、別の PR で直す。

## 86. v0.10.0 に向けた資料の改訂（12-6 の先に済ませる分）

全体の dig（internal の `docs/dig/2026-11-05-app-overall.md` の Q5）で、v0.10.0 の前に済ませると決めた資料。

- **README・README_en**: サービス化した形に書き直した（構成・置き場所、インストーラと `install.sh` での導入、ポータブル版からの移行、管理者の操作と保護された設定、定義更新と clamav-freshclam、WebUI・二段階認証・HTTPS（CA の登録の手順。Windows のストアの選び方、Firefox 系と Windows の中間証明機関、Android と Android の Firefox）・LAN からの接続（試験的）、困ったとき（E8105・起動の回数の上限・E8110・WebUI）、開発の手順）。冒頭で導入の方法が変わったことと移行の手順を目立たせた
- **CA 証明書の取り出し方**: `/web/ca.crt` も HTTPS で配るため、別の端末で開くと警告を越えることになる。README では、PC で `web tls export-ca` で書き出してファイルで渡す方法（警告を越えない）を先に書き、ブラウザでダウンロードする方法には、登録した後に `web tls renew` で証明書の例外を使われなくする手順を添えた
- **設計書**: `docs/インストーラ_パッケージング設計書.md` に 10 章、`docs/エラーコード一覧_例外設計書.md` に 8 章（段階 12 改訂）を足した。ほかの設計書（基本設計・詳細設計・画面設計・タスクトレイ常駐アプリプロセス設計・テスト仕様）の改訂は v0.10.0 の後に行う

## 87. WebUI・ほかのトレイGUI で変えた一覧が、トレイGUI の画面に出ない

#52 の Linux 実機の確認で見つかった（`dev` からあった）。WebUI で監視するフォルダを足しても、トレイGUI の「リアルタイム保護」の画面には、トレイGUI を起動し直すまで出なかった。

- 原因: `api/actions.py` は変えた後に `ApiContext.notify_changed` を呼ぶが、サービス（`api/service_api.py`）はこれを渡しておらず、何も知らせていなかった（1 プロセスの形のときは `app.py` が画面を読み直していた。12-4e で `app.py` を除いたときに抜けた）。除外・スケジュール・隔離・ログ・clamd の停止・clamav-freshclam の開始・停止も同じだった
- 直し方: サービスは `notify_changed` に `api/events.py` の `data_changed_publisher` を渡し、変えた対象を `data.changed`（`{"what": "targets"}` など。対象は `DATA_CHANGED_TARGETS`）で知らせる。設定は `config.changed`、スキャン・定義の更新の開始は `scan.started`・`update.started`、監視の開始・停止は `status` で知らせているため重ねない
- トレイGUI は `data.changed` で、その一覧を読み直す（`tray_main.reload_changed_screens`）。保存していない変更は残す。サービスに無くなった行（ほかで消した行）への保存していない変更は捨てる（`PendingEdits.prune`。保存すると E8109 になるため）。イベントを受けられなかった間（つなぎ直しの途中など）の変更も出るよう、「リアルタイム保護」「スケジュール」の画面を開いたときにも読み直す
- WebUI も、開いている画面が対象なら読み直す（`web/app.js` の `DATA_CHANGED_SCREENS`）。トレイGUI で変えたものが、開いている WebUI に出なかったのも同じ原因だった

## 88. トレイGUI とテストが一時フォルダを消さずに溜める（#55）

#54 の Linux 実機の確認で見つかった。

- トレイGUI・確認画面（`elevate_main.py`）は、数値・日付の入力欄の矢印の画像を、起動のたびに新しい一時フォルダ（`QTemporaryDir`、`/tmp/ClamQtPie-XXXXXX`）に書いていた。モジュールの変数に持たせていただけで、Python の終了のときに PySide6 のオブジェクトが壊される保証がないため、［終了］で閉じても消えずに残った。ログアウト・kill で終わったときも残る
  - 直し方: 決まった場所（ユーザー単位のキャッシュ。Windows は `%LOCALAPPDATA%\clamqtpie\theme`、Linux は `$XDG_CACHE_HOME/clamqtpie/theme`（無ければ `~/.cache/clamqtpie/theme`））に、起動のたびに上書きで書く。終わり方によらず溜まらないため、終了時の削除は要らない
  - トレイGUI と確認画面が同時に書いても壊れた画像を読まないよう、別の名前で書いてから `os.replace` で置き換える
  - pkexec の後の確認画面（root）は root のホームの下に書くため、ユーザーのフォルダに root のファイルはできない
  - フォルダを作れない・書けないときは、これまでどおり矢印なしのまま動く
- `tests/unit/test_notification_service.py` の `test_smtp_ssl_rejects_untrusted_certificate` は、試しの証明書を `tempfile.mkdtemp()` に作って消していなかった。`tmp_path` を使う
- 以前に溜まった `/tmp/ClamQtPie-*`・`/tmp/tmp*` は消さない（OS の一時フォルダの片付けに任せる）

## 89. v0.10.0 の公開

段階 12（サービス化・WebUI。§55〜§88）を v0.10.0（ベータ）として公開する。`core/version.py` の `APP_VERSION`、`version_info.txt`、`build_release.py` のビルド例を 0.10.0 にそろえた。

- 導入の方法が変わる（ポータブル版をやめ、Windows はインストーラ、Linux は `install.sh`）。v0.9.x のデータは `clamqtpie-ctl migrate`（Windows はインストーラが尋ねる）で移す
- DB は起動時に 0007 まで上がる
- 12-6 のうち、基本設計・詳細設計・画面設計・タスクトレイ常駐アプリプロセス設計・テスト仕様の改訂は v0.10.0 の後に行う（§86）

## 90. WebUI の画面のファイルのパスを前もって決める

v0.10.0 の公開側の CodeQL が、`api/web_static.py` の `static_file` を「利用者の値をパスに使っている」と指摘した（指摘は誤検知として閉じた）。名前は配るファイルの表（`FILES`）にあるかを確かめてから使っていたため、ほかのファイルは読めなかったが、要求の名前でパスを組み立てていた。

- 配るファイルのパスを読み込みのときに作っておき（`_FILE_PATHS`）、要求の名前はその表を引く鍵にだけ使う。表に無い名前は、これまでどおり 404
- 動きは変わらない

## 91. WebUI の HTTPS で受け付ける TLS の最も古い版を明示する

v0.10.0 の後の公開側の CodeQL が、テスト（`tests/unit/test_webui_https.py`）の接続する側の TLS の設定を「TLS 1.0・1.1 を許している」と指摘した。Python 3.10 以降の既定は接続する側・待ち受ける側とも TLS 1.2 以上のため、実際には古い版は使われないが、本番の WebUI の HTTPS（`api/server.py`、uvicorn）も最も古い版を自分では決めておらず、Python・OpenSSL の既定に任せていた。

- `api/server.py`: `require_modern_tls()` で、HTTPS のときは SSL の設定の `minimum_version` を `MINIMUM_TLS_VERSION`（TLS 1.2）にする。uvicorn の `ssl_context_factory` は新しい版にしか無いため、先に `config.load()` で設定を読み込んでから変える（uvicorn は読み込み済みなら読み直さない）
- テストの接続する側も TLS 1.2 以上を明示する
- テストは、決めた値がそのまま設定に入ることを、値を TLS 1.3 に変えて確かめる。TLS 1.1 で接続して断られることを確かめる形は、手元の OpenSSL 3.5 が別の設定（セキュリティレベル）でも古い版を断り、こちらの設定を外しても通ってしまったため採らなかった
