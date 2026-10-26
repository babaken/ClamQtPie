# Linuxリアルタイム監視 詳細設計書（clamonacc連携）

対象モジュール: `core/clamonacc_manager.py`（ClamonaccRealtimeManager）、`core/realtime_factory.py`（方式選択）
前提: 要件定義書2章「Linux: clamonaccを利用、clamdと連携」、詳細設計書「リアルタイム監視」シーケンス（Linux: clamonacc -> clamd -> ClamAVService -> GUI通知）、Windowsリアルタイム監視詳細設計書2章（LinuxWatcherは別設計）
作成: Phase 11（実装・Linux実機確認後に作成）

------------------------------------------------------------------------

## 1. 確認事項の反映結果

| 項目 | 決定内容 |
|---|---|
| Linuxの監視方式 | clamonacc（ClamAV公式のオンアクセススキャン）。fanotifyでファイルが開かれる前に検査でき、clamd.confの`OnAccessPrevention yes`で感染ファイルへのアクセス自体を遮断できるため（watchdog/inotifyは変更後の通知のみで遮断不可） |
| clamonaccが使えない環境 | watchdog監視（Windowsと同じRealtimeManager）へ自動フォールバックし、WARNINGログを記録する |
| root権限の取得 | pkexec（polkit）で認証する。認証は監視の開始時の1回のみ |

------------------------------------------------------------------------

## 2. 全体構成

```text
core/realtime_factory.py: create_realtime_manager()
 ├─ Windows                         → RealtimeManager（watchdog）
 └─ Linux
     ├─ realtime.linux_backend == "clamonacc" かつ clamonacc・pkexec（rootなら不要）あり
     │                              → ClamonaccRealtimeManager
     └─ それ以外                     → RealtimeManager（watchdog）＋WARNINGログ
```

- ClamonaccRealtimeManagerはRealtimeManagerと同一のインターフェースを持つ（start/stop/is_running/add_target/remove_target/on_excludes_changed、シグナル detected/watch_target_lost/scan_error、属性 config/mode_name）。GUIは方式の違いを意識しない
- 画面の方式ラベルには`mode_name`（「clamonacc」／「watchdog監視」）を表示する

------------------------------------------------------------------------

## 3. プロセス構成

```text
GUIアプリ（一般ユーザー）
 └─ pkexec /bin/sh -c <監視スクリプト> clamonacc-supervisor <clamonaccのコマンドライン>
      └─ rootシェル（監視スクリプト）
           ├─ clamonacc --foreground ...（バックグラウンド起動）
           └─ 監視サブシェル: 標準入力のEOFを待ち、EOFでclamonaccをkillする
```

監視スクリプト（`_SUPERVISOR_SCRIPT`）:

```sh
exec 3<&0; "$@" & pid=$!; ( read _unused <&3; kill "$pid" 2>/dev/null ) & wait "$pid"; echo "__CLAMONACC_EXIT__ $?"
```

- 非対話シェルでは`&`で起動したコマンドの標準入力が/dev/nullになるため、元の標準入力をfd3へ複製して監視サブシェルへ渡す
- **停止**: GUIが標準入力のパイプを閉じると、rootシェルがclamonaccを停止して終了する。停止時の再認証は不要
- **GUIの異常終了時**: パイプが閉じるためclamonaccは自動停止する（rootプロセスを取り残さない）
- **clamonaccの終了検知**: 終了マーカー`__CLAMONACC_EXIT__ <終了コード>`を標準出力で受け取る。マーカー無しでpkexecが終了コード126/127で終わった場合は「認証キャンセル・失敗」とみなす
- GUIアプリ自体がrootで動いている場合はpkexecを使わず、監視スクリプトを直接起動する

------------------------------------------------------------------------

## 4. clamonaccへの受け渡し

| 引数 | 内容 |
|---|---|
| `--foreground` | rootシェルの子として前面で動かす |
| `--log=<アプリ>/clamonacc/clamonacc.log` | 検知結果の出力先（5章）。GUIが一般ユーザー所有で事前作成し、rootは追記のみ行う |
| `--watch-list=<アプリ>/clamonacc/watch-list.txt` | 監視対象（watch_targetテーブルの有効な行、1行1パス） |
| `--exclude-list=<アプリ>/clamonacc/exclude-list.txt` | 除外フォルダ（excludeテーブルのフォルダ除外＋アプリ自身のlogs/quarantine/clamonaccフォルダ。自己検査の防止） |
| `--config-file=<パス>` | config.jsonの`realtime.clamonacc_config_file`が指定されている場合のみ |
| `--fdpass` / `--stream` | `realtime.clamonacc_fdpass`がtrueで、clamd.confに`LocalSocket`がある（またはclamd.confが見つからない）場合は`--fdpass`、それ以外は`--stream`（11章） |

- 監視対象の追加・削除・有効/無効切替、フォルダ除外の変更は、clamonaccが実行中に変更を受け付けないため**再起動で反映**する（再度認証を求められる）
- ファイル・拡張子の除外はclamonaccに渡せないため、検知後のアクション（隔離・通知）をアプリ側で行わない形で反映する（検査自体は行われる）

------------------------------------------------------------------------

## 5. 検知の取得と後続処理

- clamonaccのログを、監視開始時点の末尾から0.5秒間隔で読み、次の形式の行を検知として扱う

```text
[任意の接頭辞（時刻等）] /path/to/file: <ウイルス名> FOUND
```

- パスは最初の`/`から` : <ウイルス名> FOUND`の直前まで（パス内の`: `にも対応）。書き込み途中の最終行は次回へ持ち越す
- 監視を設定できなかったことを示す行（`ClamInotif: can't exclude`等。11章）は、監視を停止してE2005を通知する
- それ以外の`ERROR`を含む行はWARNINGログ（E2005）として記録する
- 同じファイルの検知が10秒以内に続いた場合は重複として扱い、2回目以降は通知・隔離しない（11章）
- 検知時の処理はwatchdog実装と共通: ログ記録（カテゴリ「リアルタイム」）→ `detected`シグナル（通知・トレイ表示）→ 検知時アクションが「自動隔離」ならQuarantineServiceで隔離

------------------------------------------------------------------------

## 6. 利用者側の前提設定

| 項目 | 内容 |
|---|---|
| パッケージ | clamonacc（Debian/Ubuntuでは`clamav-daemon`に含まれる）、pkexec（polkit） |
| clamd | LocalSocket（例: `/var/run/clamav/clamd.ctl`）で起動していること |
| clamd.conf（必須） | `OnAccessExcludeUname clamav`（clamd自身のファイルアクセスを検査対象から外す。`OnAccessExcludeUID`・`OnAccessExcludeRootUID yes`でも可。ローカルのclamdでは、いずれも無いとclamonaccが起動を拒否する） |
| clamd.conf（任意） | `OnAccessPrevention yes`（感染ファイルを開けなくする） |
| clamd.conf（注意） | `OnAccessMaxFileSize`（既定5MB）より大きいファイルはオンアクセススキャンされない。`OnAccessMountPath`・`OnAccessDisableDDD`は使わない（11章） |
| 監視対象 | clamdの一時フォルダ（`TemporaryDirectory`、既定`/tmp`）と`/`は監視対象にできない（11章） |

------------------------------------------------------------------------

## 7. エラー・異常系

| 事象 | 対応 |
|---|---|
| clamonacc/pkexecが見つからない | 起動時にwatchdog監視へフォールバック（WARNINGログ） |
| 監視開始時に監視対象フォルダが存在しない | 該当パスを無効化し、E2001（watch_target_lostシグナル→ダイアログ） |
| 認証キャンセル・失敗 | E2005「clamonacc（オンアクセススキャン）を実行できませんでした」（詳細「管理者認証がキャンセルされたか、失敗しました」）。監視を停止状態に戻す |
| clamonaccの異常終了（clamdに接続できない等） | E2005（詳細に終了コードとclamonaccの直近の出力）。監視を停止状態に戻す |
| clamd.confにOnAccessExcludeUname等が無い（ローカルのclamd） | 認証の前にE2005（詳細に追加すべき行。ユーザー名はclamd.confの`User`）。clamonaccを起動しない |
| clamd.confで`OnAccessDisableDDD yes`（`OnAccessMountPath`なし） | 認証の前にE2005。clamonaccを起動しない |
| clamd.confで`OnAccessMountPath`あり | WARNINGログ（E2005）を記録して起動する（マウントポイント全体を監視し、アプリの監視対象・除外は使われない） |
| 監視対象にclamdの一時フォルダ・`/`がある | そのフォルダを外して起動し、E2006「clamonaccで監視できないフォルダがあります」を通知する（監視対象の設定は残す）。監視できるフォルダが無ければ起動しない |
| clamonaccが監視を設定できない（起動後、プロセスは動き続ける） | ログの該当行を検出してclamonaccを停止し、E2005（詳細に該当行と対処方法）。監視を停止状態に戻す |
| `OnAccessPrevention yes`で感染ファイルを開けない | 自動隔離（SHA256計算時のopen）が失敗し得る。clamonaccがアクセスを遮断しているため被害は防がれる。隔離失敗はERRORログ |

------------------------------------------------------------------------

## 8. config.json 追加項目

config.jsonスキーマ定義書4.4節を参照（`linux_backend`、`clamonacc_path`、`clamonacc_config_file`、`clamonacc_fdpass`）。

------------------------------------------------------------------------

## 9. 検証結果（Phase 11）

WSL2 Ubuntu 26.04（kernel 6.18、`CONFIG_FANOTIFY_ACCESS_PERMISSIONS=y`）、ClamAV 1.5.4で`tests/integration/test_real_clamonacc.py`を実行し、以下を確認した。

- EICARファイルへのアクセスで検知され、自動隔離まで到達する
- 除外フォルダ内のファイルは検知されない
- 停止後にclamonaccプロセスが残らない
- 一般ユーザー・認証エージェント無しではpkexecが失敗し、E2005として通知される

------------------------------------------------------------------------

## 10. 未確定事項

1. polkitの認証キャッシュ（auth_admin_keep）を使って再起動時の再認証を減らすか（専用のpolkitアクション定義が必要になる）
2. systemdの`clamav-clamonacc.service`が有効な環境で、本アプリのclamonaccと二重起動しないための検出・案内

------------------------------------------------------------------------

## 11. clamonaccの仕様確認結果（Phase 11改訂、ClamAV 1.5.4のソース）

Arch Linux実機で「監視中」と表示されているのに検知しない不具合が見つかったため、ClamAV 1.5.4のclamonaccのソース（`clamonacc.c`、`fanotif/fanotif.c`、`inotif/inotif.c`、`inotif/hash.c`、`client/client.c`、`client/protocol.c`、`scan/thread.c`、`misc/utils.c`、`common/optparser.c`）と本アプリの使い方を照合した。

### 11.1 本アプリの使い方が仕様どおりであることを確認した項目

| 項目 | 根拠 |
|---|---|
| root権限（pkexec） | `fanotify_init`に必要。権限が無いとEPERMで終了する |
| `--foreground` | 無いとdaemonize()で親から切り離され、監視スクリプトから停止できない |
| `--watch-list` / `--exclude-list` | 1行1パス。各行の末尾1文字（改行）を無条件に削るため、最終行も改行で終える必要がある（本アプリは全行を改行で終えている）。存在しないパス・フォルダ以外・`/`は黙って無視される |
| 検知行 | `<パス>: <ウイルス名> FOUND`（`--fdpass`・`--stream`・パス指定のいずれも同じ形式） |
| 停止 | 監視スクリプトのkill（SIGTERM）で、clamonaccは終了処理を行って終了する |
| `OnAccessPrevention yes`での隔離失敗 | アプリのopenも検査され、感染ファイルならFAN_DENYで拒否される |

### 11.2 仕様の誤解・不足と対応

| 項目 | clamonaccの仕様 | 対応 |
|---|---|---|
| OnAccessExcludeUname | ローカルのclamd（LocalSocket、またはTCPAddrが未指定・ループバック）では、`OnAccessExcludeUID`・`OnAccessExcludeUname`・`OnAccessExcludeRootUID`のいずれも無いと終了コード2で起動を拒否する（`startup_checks()`）。初版の「推奨」は誤り | 必須に訂正。起動前（認証前）にclamd.confを確認してE2005で案内する。起動後に同じエラーで終了した場合も詳細に対処方法を付ける |
| 監視設定の失敗 | DDD（inotif.c）のスレッドは、除外・監視の登録に失敗すると`return NULL`でスレッドだけ終了し、プロセスは監視なしで動き続ける。終了マーカーでは検出できない | ログの該当行（`ClamInotif: can't include` / `can't exclude` / `could not watch path` / `issue when adding watch` / `could not add element to hash table` / `failed to initialize DDD system` / `could not parse include list` / `could not parse exclude list` / `Not watching path` / `Please specify at least one path`、`ClamMisc: could not open path list file`）を検出してclamonaccを停止し、E2005を通知する |
| clamdの一時フォルダ | `--watch-list`の処理で、clamdの`TemporaryDirectory`（既定`/tmp`）と同じパスがあると次の行へ進まず、エラー行を出力し続ける無限ループになる | 監視対象から外して起動し、E2006を通知する。`/`も同様に外す（clamonaccが無視するため） |
| 重複検知 | 隔離時のSHA256計算でアプリがファイルを開くと、clamonaccがそれも検査し同じFOUND行を再度出力する | 同じパスの検知が10秒以内に続いた場合は重複として通知・隔離しない |
| `--fdpass` | clamd.confに`LocalSocket`が無い場合は黙って無視され、clamdがパスを開いて検査する方式（CONTSCAN）になる。clamdのユーザーが読めないフォルダ（0700のホーム等）は検査できない | `LocalSocket`が無い場合と`clamonacc_fdpass: false`の場合は`--stream`（ファイルの内容を送る）を使う |
| `OnAccessMountPath` | 指定するとマウントポイント全体をfanotifyで監視し、DDDを使わないため`--watch-list`/`--exclude-list`は使われない。`OnAccessPrevention`も無効になる | WARNINGログで知らせて起動する |
| `OnAccessDisableDDD yes` | `--watch-list`は使われず、`OnAccessIncludePath`の直下のみを監視する（無ければ起動しない） | 起動前にE2005で案内する |
| `OnAccessMaxFileSize` | 既定5MB。これより大きいファイルは検査されない | 起動時にINFOログ、README・6章に記載 |

### 11.3 clamonacc側の不具合（glibc 2.44）

DDDはディレクトリの階層をglibcの`fts`で調べ、`fts_children()`の各要素が`FTS_D`（ディレクトリ）のものだけを子として記録する。Arch Linux（glibc 2.44）では`fts_children()`がすべての子を`FTS_NSOK`（stat未実施）で返すため、どのディレクトリにも子が記録されない。その結果、(1) サブフォルダの除外が`can't exclude`で失敗してDDDのスレッドが終了し、(2) 除外が無くても監視対象の直下しか監視されない。clamonaccのハッシュ表のコード（`hash.c`）を取り出して実機で再現・確認した。同じ症状がClamAV Issue #1797（not plannedでクローズ）として報告されている。

本アプリでは、(1)は11.2の監視設定の失敗として検出し、E2005の詳細でwatchdog監視（`realtime.linux_backend: "watchdog"`）への切り替えを案内する。(2)はclamonaccのログに現れないため検出できない（未確定事項3）。

### 11.4 未確定事項（追加）

3. glibc 2.44以降でclamonaccがサブフォルダを監視しない問題への対応（監視対象配下の全フォルダを`--watch-list`へ列挙する回避策、watchdog監視への自動切り替え、上流の修正待ち）

