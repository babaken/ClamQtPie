# DBマイグレーション設計書

対象モジュール: `db/database.py`（DatabaseService.migrate()）
前提: 詳細設計書1章 DatabaseServiceクラス、基本設計書4章 DB設計(SQLite)、エラーコード一覧・例外設計書E5000番台

------------------------------------------------------------------------

## 1. 確認事項の反映結果

| 項目 | 決定内容 |
|---|---|
| マイグレーションツール | Alembicを使用する |
| 起動時の適用方針 | 自動適用（確認なしで進める。事前バックアップは初版では行わない） |

------------------------------------------------------------------------

## 2. 対象DBとAlembic採用に関する補足

### 2.1 対象データ

基本設計書4章で定義された以下のSQLiteテーブルが対象。

- `quarantine`（隔離）
- `schedule`（スケジュール）
- `exclude`（除外リスト）
- `watch_target`（監視対象パス、Windowsリアルタイム監視詳細設計書3.2節で追加）

### 2.2 Alembic採用時の構成方針（SQLite向け留意事項）

- AlembicはPostgreSQL等を主眼に設計されているため、SQLiteでは`ALTER TABLE`の制約（列削除・型変更・制約変更が直接サポートされない）がある
- そのため、列削除や型変更を伴うマイグレーションでは、Alembicの`batch_operations`（`with op.batch_alter_table(...)`）を使用する。これはSQLite向けにAlembicが提供する「テーブルを再作成して差し替える」処理を自動化する仕組みであり、本アプリの全マイグレーションスクリプトで統一して使用する
- Alembicの環境設定（`alembic/env.py`）はオフラインモード（`--sql`生成）を使わず、常にオンラインモード（アプリのDB接続を直接使う`context.configure(connection=...)`方式）で実行する。理由：本アプリは配布先の各PC上でマイグレーションを実行するため、SQLスクリプトを手動適用する運用は取らない

------------------------------------------------------------------------

## 3. ディレクトリ構成

```text
db/
  database.py          # DatabaseService（接続・migrate()呼び出し口）
  models/               # 各テーブルに対応するデータクラス（既存の basic 設計に対応）
alembic/
  env.py                # オンラインモード設定
  script.py.mako
  versions/
    0001_initial.py      # quarantine/schedule/excludeの初期テーブル作成
    0002_add_watch_target.py  # watch_targetテーブル追加
    0003_add_update_history.py  # update_historyテーブル追加（Phase 6、9章）
alembic.ini
```

- 配布物（PyInstaller成果物）には`alembic/versions/`配下のマイグレーションスクリプト一式を`datas`としてバンドルする（インストーラ/パッケージング設計書3.1節の`datas`指定に追記が必要）
- `alembic.ini`の`sqlalchemy.url`はビルド時の固定値ではなく、`DatabaseService`起動時にconfig.jsonスキーマ定義書2章のポータブル配置パスから動的に組み立てる（`sqlite:///<実行ファイル配置先>/app.db`）

------------------------------------------------------------------------

## 4. app.db ファイル配置

- インストーラ/パッケージング設計書8章「未確定事項2」で保留していたDBファイル名を、本設計書にて`app.db`として正式決定する
- 配置場所: `<実行ファイル配置先>/app.db`（config.jsonと同階層、ポータブル構成に統一）
- **権限（Phase 11改訂・セキュリティレビュー）**: app.dbには隔離した元ファイルのパス・ウイルス名・監視対象フォルダ等が入るため、接続時にapp.dbと付随ファイル（`-wal`/`-shm`/`-journal`）を所有者のみ読み書き可（0600）にする。存在しなければ0600で作成してから接続し、過去バージョンで作られた緩い権限（0644等）のDBも接続時に直す。POSIXのみ（Windowsはパーミッションの仕組みが異なり、アプリのフォルダも利用者管理下のため対象外）。`config.json`は`ConfigService`が同様に所有者のみ読み書き可で書き出す

------------------------------------------------------------------------

## 5. 起動時マイグレーションフロー

### 5.1 処理シーケンス

```text
app.py 起動シーケンス（タスクトレイ常駐アプリプロセス設計書8章 ステップ5に相当）
 → DatabaseService.connect()
      ├─ app.dbが存在しない場合
      │    → 新規作成し、Alembicの最新リビジョンまで全マイグレーションを順次適用
      │      （実質的に「初期スキーマ作成」として機能する。個別テーブルCREATE文は
      │        持たず、Alembicのマイグレーション適用のみで完結させる）
      │
      └─ app.dbが存在する場合
           → 現在のリビジョン（alembic_versionテーブル）を確認
           → 最新リビジョンと異なる場合、自動的に不足分のマイグレーションを順次適用
           → 適用完了後、通常の接続処理へ進む
 → DatabaseService.migrate() 完了 → 後続の起動シーケンスへ
```

### 5.2 自動適用（確認なし）の実装方針

- ユーザーへの確認ダイアログは表示せず、`DatabaseService.migrate()`内で`alembic.command.upgrade(alembic_cfg, "head")`相当の処理を同期的に実行してからアプリ起動を継続する
- 適用中はスプラッシュ表示等は設けない（初版のマイグレーション件数は少なく、処理時間は数百ms〜数秒程度と想定されるため）。マイグレーション件数が将来的に増え体感できる遅延が生じる場合は、進捗表示の追加を検討課題とする（8章）

### 5.3 適用失敗時の挙動（エラーコードE5002との対応）

- マイグレーション適用中に例外が発生した場合、エラーコード一覧のE5002（マイグレーション失敗）としてハンドリングする
- 事前バックアップを取らない方針（1章）のため、失敗時のDB状態は「一部マイグレーションが適用され中途半端な状態」になり得る。この場合：
  1. `DatabaseMigrationError`（E5002）をログにERRORとして詳細記録（失敗したリビジョン、SQLエラー内容）
  2. アプリ起動を中断し、エラーダイアログ「データベースの更新に失敗しました。エラーコード: E5002」を表示（エラーコード一覧5.1節の共通フォーマット）
  3. ダイアログのメッセージに「`app.db`を`app.db.corrupted`等にリネームして再起動すると初期状態から開始できますが、既存の隔離・スケジュール・除外設定は失われます」という復旧手段の案内文を追加する（バックアップがないため、これが唯一のユーザー側リカバリ手段になる点に留意）

------------------------------------------------------------------------

## 6. マイグレーションスクリプト作成ルール

### 6.1 命名規則

`alembic/versions/<4桁連番>_<内容を表す英語スラッグ>.py`（例: `0001_initial.py`, `0002_add_watch_target.py`）。Alembic標準のハッシュ付きリビジョンIDに加え、ファイル名で内容が分かるようにする。

### 6.2 1マイグレーション1目的の原則

- 1つのマイグレーションスクリプトには単一の変更目的のみを含める（例: 「テーブル追加」と「既存列のデフォルト値変更」を同一スクリプトに混在させない）。障害発生時の影響範囲特定と、E5002発生時のログ調査を容易にするため

### 6.3 データマイグレーション（スキーマ変更に伴うデータ移行）を含む場合の注意

- 例えば将来`exclude.type`の値セットを変更するような、スキーマだけでなく既存データの書き換えを伴う変更が発生した場合、`op.execute()`でのUPDATE文もマイグレーションスクリプト内に含めてよいが、大量データを対象とする処理は事前バックアップなしの自動適用（5.2節）とは相性が悪いため、そのようなマイグレーションが必要になった時点で1章の方針（バックアップなし自動適用）を再検討する

------------------------------------------------------------------------

## 7. 初期リビジョン（0001_initial）の内容例

```python
"""initial schema

Revision ID: 0001
"""
from alembic import op
import sqlalchemy as sa

def upgrade():
    op.create_table(
        'quarantine',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('uuid', sa.Text),
        sa.Column('original_path', sa.Text),
        sa.Column('virus_name', sa.Text),
        sa.Column('sha256', sa.Text),
        sa.Column('size', sa.Integer),
        sa.Column('detected_at', sa.Text),
        sa.Column('restored', sa.Integer),
        sa.Column('deleted', sa.Integer),
    )
    op.create_table(
        'schedule',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('name', sa.Text),
        sa.Column('enabled', sa.Integer),
        sa.Column('cron', sa.Text),
        sa.Column('target', sa.Text),
        sa.Column('scan_type', sa.Text),
        sa.Column('last_run', sa.Text),
        sa.Column('next_run', sa.Text),
    )
    op.create_table(
        'exclude',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('type', sa.Text),
        sa.Column('value', sa.Text),
        sa.Column('enabled', sa.Integer),
    )

def downgrade():
    op.drop_table('exclude')
    op.drop_table('schedule')
    op.drop_table('quarantine')
```

## 8. 追加リビジョン（0002_add_watch_target）の内容例

```python
"""add watch_target table

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

def upgrade():
    op.create_table(
        'watch_target',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('path', sa.Text),
        sa.Column('enabled', sa.Integer),
        sa.Column('added_at', sa.Text),
    )

def downgrade():
    op.drop_table('watch_target')
```

------------------------------------------------------------------------

## 9. downgrade（ロールバック）方針

- 本アプリの配布形態（各PC上で個別にDBを保持するポータブル構成）では、リリース後にdowngradeを実運用で使う想定は薄いが、Alembicの規約に従い全マイグレーションスクリプトに`downgrade()`を実装しておく（開発時のテスト・手戻り対応のため）
- アプリ本体からdowngradeを呼び出す機能（GUIからの「以前のバージョンに戻す」操作等）は初版では提供しない

------------------------------------------------------------------------

## 9.1 追加リビジョン（0003_add_update_history）

画面設計書9章「未確定事項2」（定義更新履歴を専用テーブルで持つか、ログから都度集計するか）をPhase 6で専用テーブル方式に決定し、追加した（Phase 11で本書へ反映）。

```sql
CREATE TABLE update_history (
    id INTEGER PRIMARY KEY,
    executed_at TEXT,      -- 実行日時（UTC ISO8601）
    result TEXT,           -- "success" | "failure" | "no_change"
    before_version TEXT,   -- 更新前の定義DBバージョン
    after_version TEXT     -- 更新後の定義DBバージョン
);
```

- 記録元: UpdateService.update_now()（定義更新画面・ホーム画面・トレイ・スケジュールジョブ・自動更新のいずれから実行しても1行記録）
- downgradeは`DROP TABLE update_history`

------------------------------------------------------------------------

## 10. 未確定事項（次工程で決定が必要）

1. 事前バックアップなしで自動適用する方針（1章）について、マイグレーション失敗時の復旧手段がユーザー任せ（5.3節）になる点のリスクを許容してよいか、リリース前に改めて確認したい
2. マイグレーション件数増加時の進捗表示追加（5.2節）の要否・実装タイミング
3. Alembicパッケージ（および依存のSQLAlchemy）がPyInstallerバンドル時にサイズへ与える影響（インストーラ/パッケージング設計書のサイズ最適化方針との整合確認）
