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
- 数値・日付の入力欄（QSpinBox / QDoubleSpinBox / QDateEdit / QTimeEdit）の上下ボタンが暗い背景に埋もれて見えにくかった（利用者の確認で指摘）。入力欄の背景・枠線をスタイルシートで指定したため、上下ボタンが小さな暗い四角になり、矢印も描かれていなかった。上下ボタンの背景・区切り線・ホバー/押下の色を指定し、矢印はQPainterで描いた三角形の画像（`gui/theme.py`の`_spin_arrow_rules()`）を`image: url(...)`で指定する。上下ボタンをスタイルシートで指定するとQtは矢印を描かず、枠線で三角形を作る指定も四角形になるため画像にした。画像はアイコンと同じく同梱せず、起動時に一時フォルダ（QTemporaryDir。終了時に削除）へ書く。無効時は灰色の矢印。プルダウン（QComboBox）の矢印は見えているため変えていない

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
