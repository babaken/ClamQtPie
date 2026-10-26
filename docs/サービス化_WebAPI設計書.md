# サービス化・Web API 設計書（下書き）

対象: サービス本体（新規 `service_main.py`、`api/`）、トレイGUI（`gui/`、API クライアント化）、ブラウザ WebUI（新規 `web/`）
前提: Phase 11 までの `core/`・`db/` の構成、clamav-webui（FastAPI 版 ClamAV 管理 UI）の画面と安全対策
作成: Phase 12 計画時（下書き。計画時の未確定事項はすべて決定済み、12章）

------------------------------------------------------------------------

## 1. 目的と決定事項

| 項目 | 決定内容 |
|---|---|
| 目的 | ログインしていなくてもリアルタイム監視・スケジュールスキャン・定義更新を動かす。トレイGUIとブラウザの両方から同じ機能を操作する |
| 実行形態 | `core/`・`db/` を **サービス本体**（Windows サービス／systemd デーモン）で動かす。画面はサービスから切り離し、**トレイGUI** と **ブラウザ WebUI** が同じ Web API を呼ぶ |
| 単体起動（従来の1プロセス形態） | 残さない。GUI は常にサービスへ接続する（同じ処理を2通り保守しないため）。USB などからのポータブル利用はできなくなる（利用者の判断、12章 16） |
| 配布形式 | ポータブル ZIP をやめる。Windows は Inno Setup のインストーラ、Linux は tar.gz＋導入スクリプト（利用者の判断、9.3） |
| WebUI の扱い | clamav-webui の画面・ミドルウェアを流用し、バックエンドは ClamQtPie の `core/` を呼ぶ。clamav-webui 独自の `scanner.py`／`scheduler_*.py` などは使わない |
| WebUI の ON/OFF | トレイメニューから切り替える。OFF の間はブラウザ向けの入口（静的ファイル・Cookie セッション）を閉じる。GUI 向け API は常に有効 |
| WebUI のログイン | 固定のアカウント1組（ユーザー名・パスワード）のみ。権限は admin（全操作）。ユーザー名の初期値は `admin`。ユーザー名・パスワードはトレイメニューの「WebUI ▶ ログイン設定」か、GUI のない環境では管理用コマンド `clamqtpie-ctl`（3.4）からだけ変更でき、WebUI にはアカウントの追加・削除・変更の機能を置かない |
| LAN からの接続 | 許可できる（既定は OFF）。初版は HTTP のままパスワード認証のみ（SSH・HTTPS は使わない）。WebUI の待ち受けを内部 API と分け、LAN に出すのは WebUI だけにする（3.1） |
| 通知 | 未読・既読をサービスで1か所に持つ。トレイGUI のトーストに加え、WebUI が起動していればブラウザでも通知を確認して消せる（7章） |
| 権限 | サービスは Windows: LocalSystem、Linux: root。トレイGUI は一般ユーザー権限で動く（`uac_admin=True` をやめる） |

------------------------------------------------------------------------

## 2. 全体構成

```text
┌──────────────── サービス本体（SYSTEM / root、画面なし） ────────────────┐
│ QCoreApplication（メインスレッド）                                     │
│   core/: ClamAVService, ScanDispatcher, RealtimeManager／Clamonacc…,   │
│          SchedulerService, UpdateService, QuarantineService,           │
│          ExcludeService, WatchTargetService, NotificationService(メール)│
│   db/:   DatabaseService (app.db)                                       │
│   api/:  QtInvoker（API スレッド → メインスレッドの橋渡し）             │
│          EventHub（Signal → SSE 配信、未表示通知の保存）                │
│ uvicorn スレッド（asyncio）                                             │
│   api/: FastAPI アプリ  /api/v1/*  /events  /web/*                     │
└──────────────▲───────────────────────────────▲─────────────────────────┘
               │ HTTP + Bearer トークン          │ HTTP + Cookie セッション + CSRF
               │ (127.0.0.1:8765 のみ)            │ (:8766、WebUI ON の時のみ。LAN 許可可)
┌──────────────┴──────────────┐       ┌──────────┴──────────┐
│ トレイGUI（一般ユーザー）     │       │ ブラウザ WebUI       │
│ gui/ 10画面・トレイ・トースト │       │ web/index.html,app.js│
│ ApiClient / サービスプロキシ  │       │                      │
└─────────────────────────────┘       └──────────────────────┘
```

- `core/` は QtCore のみに依存し QtWidgets を使わないため、`QCoreApplication` 上でそのまま動く（Phase 12 調査で確認）。
- デスクトップ通知（トースト／notify-send）はサービスでは出せない（Windows の session 0 分離、Linux はユーザーセッションなし）。サービスは通知イベントを配信・保存し、トレイGUI と WebUI が表示し、どちらからでも既読にできる（7章）。

------------------------------------------------------------------------

## 3. 通信経路と認証

### 3.1 待ち受け

待ち受けは **2つに分ける**。GUI・管理コマンド向けの API（Bearer トークン）は LAN に出さず、LAN に出すのはブラウザ向けの WebUI だけにする。

| | 内部 API | WebUI |
|---|---|---|
| 用途 | トレイGUI、`clamqtpie-ctl` | ブラウザ |
| アドレス | `127.0.0.1` のみ（変更不可） | 既定 `127.0.0.1`。「LAN からの接続を許可」で `0.0.0.0`（または `api.web_bind_address` で指定したアドレス） |
| ポート | `8765`（`api.port`） | `8766`（`api.web_port`） |
| 受け付ける認証 | Bearer トークンのみ（Cookie セッションは 401） | Cookie セッションのみ（Bearer トークンは 401） |
| 提供するもの | `/api/v1/*`、`/api/v1/events` | `/web/*`、`/api/v1/auth/session`、ログイン後は `/api/v1/*` と `/api/v1/events`（`PUT /web`・`PUT /web/credentials` を除く） |
| 起動 | 常時 | WebUI が ON のときだけ。OFF にしたら待ち受け自体を止める |

- 2つの uvicorn サーバーを同じ asyncio ループで動かし、FastAPI のルーターは共通にする。どちらの待ち受けから来た要求かで、受け付ける認証方式を切り替える。
- Linux の追加経路: なし。UNIX ソケット＋`SO_PEERCRED` は使わず、Windows と同じ TCP＋トークンにそろえる（12章 3）。

検査（clamav-webui のミドルウェアを拡張して流用）:

| 項目 | 内部 API | WebUI（LAN 許可時） |
|---|---|---|
| 接続元 IP | `127.0.0.1`／`::1` のみ | ループバックと **プライベートアドレスのみ**（`10.0.0.0/8`、`172.16.0.0/12`、`192.168.0.0/16`、`169.254.0.0/16`、`fc00::/7`、`fe80::/10`）。それ以外は接続直後に 403。`api.web_allowed_networks` で狭められる（例: `["192.168.1.0/24"]`） |
| Host 検査 | `127.0.0.1:<port>`／`localhost:<port>` のみ | 上記＋このPC のホスト名・各ネットワークインターフェースの IP アドレス＋`api.web_allowed_hosts`。それ以外は 400（DNS リバインディング対策） |
| Origin 検査 | 状態を変えるメソッドで `Origin`／`Referer` が localhost であること | `Origin`／`Referer` が `Host` と同じであること |

LAN からの接続を許可するときの注意（HTTP のまま）:

- 初版は **HTTPS にしない**（利用者の判断）。そのため、同じ LAN で通信を盗み見られると **パスワードと Cookie セッションが読み取られる**。家庭内など信頼できる LAN だけで使う前提とし、次の対策をとる。
  - LAN 許可を ON にするとき、トレイGUI・`clamqtpie-ctl` の両方で「通信は暗号化されません」の確認を出す。
  - LAN から開いたログイン画面と WebUI の上部に、暗号化されていない旨の警告を常に表示する。
  - LAN 許可中はパスワードを **12 文字以上** 必須にする（localhost のみなら 8 文字以上のまま）。短いパスワードのまま LAN 許可を ON にしようとしたら、先にパスワードの変更を求める。
  - セッションの有効期限を短くする（LAN 許可中は 15 分）。ログイン・ログアウト・ログイン失敗を、接続元 IP 付きでログに残す。
- HTTPS 対応は次の段階で行う（3.5、段階 12-7）。
- ファイアウォール: Windows は LAN 許可を ON にしたときに受信規則（`ClamQtPie WebUI`、TCP `api.web_port`、リモートアドレス `LocalSubnet`、プロファイルはプライベートのみ）を `netsh advfirewall` で追加し、OFF で削除する。Linux（ufw／firewalld）は自動では変えず、`clamqtpie-ctl web status` で必要な設定を案内する。

### 3.2 ロールとトークン

API に認証がないと、同じ PC の別ユーザーやマルウェアが SYSTEM 権限の操作（隔離の復元、除外の追加、監視の停止）をできてしまう。そのためロールを2つに分け、トークンで認証する。

| ロール | できること | トークンファイル | 読める人 |
|---|---|---|---|
| `user` | 状態・一覧・ログの閲覧、手動スキャン、定義更新、イベント購読、通知の受信と既読 | Windows: `%ProgramData%\ClamQtPie\api\user.token`／Linux: `/var/lib/clamqtpie/api/user.token` | Windows: ローカルグループ `ClamQtPie Users`／Linux: グループ `clamqtpie`（0640） |
| `admin` | `user` の全操作＋隔離の復元・削除、除外・監視対象・スケジュール・設定の変更、リアルタイム監視の ON/OFF、clamd の起動・停止、ログ消去、WebUI の ON/OFF | `…\api\admin.token` | Windows: `SYSTEM`・`Administrators`／Linux: root（0600） |

- トークンはサービス起動時に毎回 32 バイトの乱数から作り直す（`secrets.token_urlsafe(32)`）。比較は `hmac.compare_digest`。
- **トレイGUI を使えるのは管理者だけ**（利用者の判断、12章 5）。インストーラが `ClamQtPie Users`（Linux は `clamqtpie`）グループを作り、Administrators（Linux は `sudo`／`wheel`）の利用者を登録する。トークンファイルを Administrators グループに直接読ませないのは、Windows では UAC で昇格していない管理者のトークンの Administrators SID が「拒否専用」になり、許可の ACL が効かないため。
  - グループに入っていない利用者がトレイGUI を起動したら、「この PC の ClamQtPie を使う権限がありません」と表示して終了する。通知のトースト・ログ・隔離一覧（検知したファイル名を含む）も、一般の利用者には見えない。
  - 管理者を後から追加した場合は `clamqtpie-ctl users add <名前>` でグループに登録する（中身は `net localgroup`／`usermod -aG`）。
- GUI は起動時に `user.token` を読む。admin の操作は **操作したときだけ** 昇格を求める（12章 1）。
  - Windows: `ClamQtPieElevate.exe`（`uac_admin=True` の小さな exe）を起動し、UAC の確認後に admin.token を標準出力で受け取る。
  - Linux: `pkexec` で同じ役割のヘルパーを起動する。
  - 受け取った admin トークンは GUI のメモリにだけ保持し、最後の admin 操作から 15 分で破棄する。サービスの再起動でトークンが変わった場合（401）も破棄し、次の admin 操作で再度昇格を求める。
  - 閲覧・手動スキャン・定義更新・通知の既読は user ロールで行えるので、昇格の確認は出ない。
- ヘッダ: `Authorization: Bearer <token>`。
- ここでのロールは、トレイGUI がサービスに接続するための内部の区別で、ログインするユーザーではない。WebUI のログインアカウントは 3.3 の固定アカウント1組だけで、`user` 相当のアカウントは作らない。

### 3.3 ブラウザ（WebUI）のログイン

固定のアカウント1組（ユーザー名・パスワード）でログインする。セッションの権限は常に admin。

```text
ブラウザ → GET /web/login  （ユーザー名・パスワードの入力画面）
ブラウザ(app.js) → POST /api/v1/auth/session {"username": "admin", "password": "..."}
      ← Set-Cookie: session=<乱数>; HttpOnly; SameSite=Strict; Path=/
         Set-Cookie: csrf_token=<乱数>; SameSite=Strict; Path=/
```

ログインアカウント:

- ユーザー名の初期値は `admin`。**パスワードの初期値は持たない**。既定のパスワードは誰でも知っているため、初めて WebUI を ON にするときに設定させる。
- 設定はトレイメニューの「WebUI ▶ ログイン設定...」のダイアログ（ユーザー名、パスワード、確認入力）から行う。API は `PUT /api/v1/web/credentials` で、**admin の Bearer トークンでのみ受け付ける**。Cookie セッション（ブラウザ）からの要求は 403（E8102）で拒否する。WebUI の画面にはアカウントの追加・削除・変更の項目を置かない。
- パスワードは `hashlib.scrypt`（ソルト 16 バイト、n=2^14, r=8, p=1）のハッシュだけを `api.web_password_hash` に保存する。平文はどこにも残さない。標準ライブラリのみで実装し、依存を増やさない。
- パスワードは 8 文字以上（LAN 許可中は 12 文字以上、3.1）。ユーザー名は 1〜32 文字の英数字と `_.-`。
- ユーザー名・パスワードを変更したら、既存の Cookie セッションをすべて破棄する。
- パスワードが未設定の間は WebUI を ON にできない（`PUT /web` は 400）。トレイの「WebUI ▶ WebUI ON/OFF」は、未設定なら先にログイン設定のダイアログを開く。

ログインの保護:

- 照合は `hmac.compare_digest` で行う。ユーザー名が違う場合もパスワード照合と同じ時間をかけ、どちらが違ったかを応答で区別しない。
- 同じ接続元 IP から5回続けて失敗したら、その IP からのログインを 5 分間受け付けない（429、E8106）。接続元を変えて試される場合に備え、全体でも 1 時間に 30 回失敗したら 15 分間すべてのログインを止める。失敗とロックは接続元 IP 付きでログに残す。
- セッションの有効期限は最終操作から 30 分（`api.session_timeout_min`。LAN 許可中は 15 分）。

共通:

- Cookie セッションの状態変更リクエストには、clamav-webui と同じダブルサブミット方式の CSRF 検査（`csrf_token` Cookie と `X-CSRF-Token` ヘッダの一致）を行う。Bearer 認証のリクエストは CSRF 検査の対象外。
- WebUI が OFF のときは `/web/*`・`/api/v1/auth/session` は 404、既存の Cookie セッションはすべて破棄する。

### 3.4 管理用コマンド `clamqtpie-ctl`（GUI のない環境向け）

GUI のない Linux サーバーではトレイメニューを使えないため、WebUI の ON/OFF とログイン設定を端末から行うコマンドを用意する。Windows（Server Core など）でも同じコマンドを使える。

```text
clamqtpie-ctl web status                          WebUI の状態（ON/OFF、URL、ユーザー名、パスワード設定済みか）
clamqtpie-ctl web enable | disable                WebUI を ON / OFF にする
clamqtpie-ctl web lan on | off [--yes]            LAN からの接続を許可する / やめる（暗号化されない旨の確認あり。--yes で省略）
clamqtpie-ctl web lan allow CIDR...               接続を許可するネットワークを指定する（例: 192.168.1.0/24。省略時はプライベートアドレス全体）
clamqtpie-ctl web set-login [--username NAME]     ユーザー名とパスワードを設定する（パスワードは2回入力、画面に表示しない）
                            [--generate]          パスワードを自動生成して設定し、生成したパスワードを1回だけ表示する
                            [--password-stdin]    パスワードを標準入力から1行読む（構成管理ツール・スクリプト用）
clamqtpie-ctl web hash-password                   パスワードを入力し、保存形式のハッシュだけを出力する（設定は変えない）
clamqtpie-ctl status                              サービスの状態、待ち受けポート、clamd、スリープ解除タイマーの可否（9.1）
clamqtpie-ctl users add | remove NAME             トレイGUI を使える利用者（ClamQtPie Users／clamqtpie グループ）に追加・削除する
clamqtpie-ctl config set api.port N               内部 API のポートを変える（サービスの再起動が必要、9.4）
clamqtpie-ctl migrate --from DIR                  旧版（ポータブル）のデータを移す（9.2）
```

- 実行には root（Windows は管理者）が必要。`admin.token` を読み、稼働中のサービスへ `PUT /web`・`PUT /web/credentials` を送る。サービスの処理（既存セッションの破棄、入力の検証）をそのまま通すため、設定ファイルを直接書き換えない。
- サービスが停止中のときだけ、`ConfigService` で設定ファイルを直接更新する（起動時に読まれる）。
- `--generate` のパスワードは `secrets` で生成する。20文字、英大文字・英小文字・数字から、紛らわしい文字（`0 O o 1 l I`）を除いたもの。表示は端末にだけ行い、ログ・シェル履歴には残さない。
- パスワードをコマンドライン引数で渡す方法（`--password xxx`）は用意しない。`ps` やシェル履歴から見えてしまうため。
- `web enable` は、パスワードが未設定ならエラーにし、`set-login` の実行を案内する。
- 初回導入（`install_linux.sh` 相当のインストーラ）では、WebUI は OFF のまま導入する。ON にするには `set-login` → `enable` の順で実行する。
- 実体は `service_main.py` と同じパッケージに入れ、PyInstaller で `clamqtpie-ctl`（Windows は `clamqtpie-ctl.exe`、console=True）として出力する。

GUI のない Linux サーバーを LAN の別の PC から使う手順:

```bash
sudo clamqtpie-ctl web set-login --generate     # 12文字以上（生成は20文字）
sudo clamqtpie-ctl web lan on
sudo clamqtpie-ctl web enable
# 別の PC のブラウザで http://<サーバーのIP>:8766/web/login を開く
```

### 3.5 HTTPS 対応（次の段階、12-7）

方式: **ClamQtPie 専用のローカル認証局（CA）を作り、その CA で WebUI のサーバー証明書を発行する**（mkcert と同じ方式）。ブラウザを使う端末に CA 証明書を一度だけ登録すれば、警告なしの通常の HTTPS として扱われ、ブラウザの通知（Notification API、HTTPS が必須）も使える。

自己署名の証明書を1枚使う方式は採らない。理由:

- 開くたびに証明書の警告が出る。警告を無視して進んだページでは、ブラウザによって一部の機能（Service Worker など）が止まり、通知が使えるかも保証できない。
- IP アドレスが変わって証明書を作り直すたびに、すべての端末で警告が出直す。ローカル CA なら端末が信頼するのは CA なので、サーバー証明書を作り直しても端末側の作業はいらない。

証明書と鍵:

| もの | 内容 | 置き場所・権限 |
|---|---|---|
| CA の鍵・証明書 | 初回の HTTPS 有効化時に1回だけ作る。ECDSA P-256、有効期間 10 年。CN は `ClamQtPie Local CA (<ホスト名>)` | `<設定ディレクトリ>/tls/ca.key`（SYSTEM／root のみ、Linux は 0600）、`ca.crt` |
| サーバー証明書 | CA で署名。ECDSA P-256、有効期間 1 年。SAN に「このPC のホスト名（短い名前と FQDN）、`localhost`、各ネットワークインターフェースの IP アドレス、`api.web_allowed_hosts`」を入れる | `<設定ディレクトリ>/tls/server.key`（SYSTEM／root のみ）、`server.crt` |

- **CA 鍵の悪用を防ぐ。** CA を登録した端末は、この CA が署名した証明書をどのサイトのものでも信頼してしまう。そのため CA 証明書に **Name Constraints**（critical）を付け、発行できる名前をプライベートアドレス（3.1 の範囲）・`localhost`・このPC のホスト名・`.local`・`.lan`・`.home.arpa` と `api.web_allowed_hosts` に限る。CA 鍵はサーバー証明書の発行以外に使わず、`clamqtpie-ctl` からも読み出せないようにする。
- **自動更新。** サービス起動時と1日1回、サーバー証明書の SAN と現在の IP アドレス・ホスト名を比べ、違っていれば（または期限まで 30 日を切ったら）作り直して WebUI の待ち受けを再起動する。
- **CA の作り直し。** `clamqtpie-ctl web tls reset-ca` で CA ごと作り直せる（鍵が漏れた疑いがある場合など）。作り直したら端末への登録もやり直しになる旨を確認する。
- **利用者が用意した証明書。** `api.web_tls_cert`／`api.web_tls_key` を指定した場合はそれを使い、ローカル CA と自動更新は使わない。
- 依存の追加: `cryptography`（証明書の作成。標準ライブラリの `ssl` では作れない）。PyInstaller の spec にも反映する。

CA 証明書の配布（公開してよい部分のみ。鍵は配らない）:

- WebUI の `http://<IP>:<web_port>/ca.crt`（HTTP のまま取得できる入口。証明書以外は何も提供しない）と、HTTPS 有効後の WebUI の設定画面からダウンロード。
- トレイメニュー「WebUI ▶ CA 証明書を保存...」、`clamqtpie-ctl web tls export-ca [ファイル]`。
- 取得画面に CA 証明書の SHA-256 フィンガープリントを表示し、登録前にトレイGUI や `clamqtpie-ctl web tls status` の表示と照合するよう案内する（LAN 上で証明書をすり替えられていないことの確認）。
- 登録手順（Windows、macOS、iOS、Android、Linux の Firefox・Chrome）はマニュアルに書く。

HTTPS 有効時の変更:

- WebUI の待ち受けを HTTPS にする（ポートは `api.web_port` のまま）。`ca.crt` の配布だけは別ポート（`api.web_ca_port`、既定 8767）の HTTP で提供し、ほかの要求はすべて HTTPS へ転送する。
- Cookie に `Secure` を付ける。HSTS は付けない（IP アドレスで開く LAN 用途で、HTTPS を切ったときに開けなくなるため）。
- 3.1 の「暗号化されません」の確認と警告表示は出さない。LAN 許可中のパスワード 12 文字以上と、接続元 IP の制限・総当たり対策は残す。
- ブラウザの OS 通知（7.3）を有効にする。
- 内部 API（`127.0.0.1:8765`）は HTTP のまま変えない。

設定（`api` に追加）: `web_tls`（`"off"` | `"local_ca"` | `"custom"`、初期値 `"off"`）、`web_tls_cert`、`web_tls_key`、`web_ca_port`。

管理用コマンド（3.4 に追加）:

```text
clamqtpie-ctl web tls on | off           HTTPS を有効 / 無効にする（初回はローカル CA を作る）
clamqtpie-ctl web tls status             方式、CA とサーバー証明書の有効期限・SAN・フィンガープリント
clamqtpie-ctl web tls export-ca [FILE]   CA 証明書を書き出す（省略時は標準出力へ PEM）
clamqtpie-ctl web tls use-cert CERT KEY  利用者が用意した証明書を使う
clamqtpie-ctl web tls reset-ca           CA を作り直す（全端末で登録のやり直しが必要）
```

------------------------------------------------------------------------

## 4. 共通仕様

### 4.1 形式

- ベースパス `/api/v1`。JSON（UTF-8）。日時は ISO 8601 のタイムゾーン付き（DB の既存値と同じく UTC で保存し、表示時に GUI／ブラウザがローカル時刻へ変換）。
- 一覧は `?limit=`（既定 100、最大 1000）・`?offset=` を受け付け、`{"items": [...], "total": n}` を返す。
- ID は DB の整数 ID をそのまま使う。

### 4.2 エラー

`AppError`（`code`／`message`／`detail`）をそのまま返す。

```json
{"error": {"code": "E3002", "message": "復元先に同じ名前のファイルがあります", "detail": "C:\\Users\\a\\x.exe"}}
```

| HTTP | 条件 |
|---|---|
| 400 | 入力検証エラー（`ValueError`、E4xxx のスケジュール検証、E6xxx の設定検証など） |
| 401 | トークン・セッションなし、または不正（E8101） |
| 403 | ロール不足（E8102）、CSRF・Origin・Host 検査の失敗（E8103） |
| 404 | 対象が存在しない（例: E3001 隔離データなし）、WebUI OFF 時の `/web/*` |
| 409 | 実行中のため受け付けられない（E8104: スキャン実行中、定義更新実行中、同じジョブを実行中） |
| 429 | ログインの失敗が続いたため一時的に受け付けない（E8106） |
| 503 | clamd に接続できない（E1xxx） |
| 500 | 上記以外の `AppError`・予期しない例外（E9001。`detail` はログにだけ残し、応答には含めない） |

追加するエラーコード（E8100番台: API。エラーコード一覧・例外設計書に追記する）:

| コード | 例外クラス | メッセージ |
|---|---|---|
| E8100 | `ApiError` | API の処理でエラーが発生しました |
| E8101 | `ApiAuthError` | 認証が必要です |
| E8102 | `ApiForbiddenError` | この操作には管理者の権限が必要です |
| E8103 | `ApiRequestRejectedError` | 不正なリクエストのため拒否しました |
| E8104 | `ApiConflictError` | 別の処理を実行中のため受け付けられません |
| E8105 | `ServiceUnavailableError` | ClamQtPie サービスに接続できません（GUI 側で使用） |
| E8106 | `ApiLoginLockedError` | ログインの失敗が続いたため、しばらく時間をおいてください |
| E8107 | `ServiceVersionMismatchError` | サービスとトレイGUI の版数が一致しません。再インストールしてください（GUI 側で使用、9.4） |

### 4.3 時間のかかる処理

スキャンと定義更新は **202 Accepted ＋ジョブ** の形にする。開始要求はすぐ返し、進捗はイベント（5章）か `GET` で取る。

```json
{"job": {"id": "scan-20261026-031500-1", "kind": "scan", "state": "running", "started_at": "...", "progress": {...}}}
```

`state`: `running` → `finished` | `cancelled` | `failed`。終了したジョブは種類ごとに直近1件を保持する。

------------------------------------------------------------------------

## 5. イベント配信（SSE）

`GET /api/v1/events`（`user` 以上）。`text/event-stream` で次のイベントを送る。各イベントに連番の `id` を付け、直近 1,000 件をメモリに保持する。再接続時は `Last-Event-ID` 以降を再送する。

| event | data | 発生元 |
|---|---|---|
| `status` | `GET /status` と同じ内容 | 状態が変わったとき（監視 ON/OFF、clamd の状態、実行中フラグ） |
| `scan.progress` | `{job_id, scanned, total, current_path}` | `ScanDispatcher.progress`（**1秒に最大2回に間引く**） |
| `scan.file` | `{job_id, path, status, detail, detected_at}` | `ScanDispatcher.file_scanned`（`status` が検知・エラーのものだけ） |
| `scan.activity` | `{job_id, message, level, count}` | `ScanDispatcher.activity` |
| `scan.finished` | `{job_id, scanned, detected, errors, cancelled}` | `ScanDispatcher.finished` |
| `realtime.detected` | `{path, virus_name}` | `detected` |
| `realtime.handled` | `{path, virus_name, quarantined}` | `detection_handled` |
| `realtime.target_lost` | `{path}` | `watch_target_lost` |
| `realtime.error` | `{code, message}` | `scan_error` |
| `update.log` | `{job_id, line}` | `UpdateDispatcher.log_line` |
| `update.finished` | `UpdateHistoryRecord` | `UpdateDispatcher.finished` |
| `schedule.finished` | `{job_id, name, result, note}` | `SchedulerService` の最終実行結果の記録時 |
| `notification` | `{id, created_at, title, body, open_screen}` | デスクトップ通知の要求（7章） |
| `notification.acked` | `{ids: [...]}` | 通知が既読になったとき（トレイGUI・WebUI のどちらで消しても、もう片方の表示から消す） |
| `config.changed` | `{}` | 設定の保存後（GUI・ブラウザが画面を読み直す） |

------------------------------------------------------------------------

## 6. エンドポイント一覧

ロール欄: U = `user` 以上、A = `admin`。

### 6.1 状態・情報

| メソッド・パス | ロール | 呼び出す処理 | 内容 |
|---|---|---|---|
| `GET /version` | なし | `core/version.py` | アプリ版数、API 版数（`"v1"`）。接続確認にも使う |
| `GET /status` | U | `ClamAVService`、`realtime_manager`、`UpdateService`、`SchedulerService` | 下記 |
| `POST /clamd/start` | A | `ClamdStarter` | clamd の起動を試みる（バックグラウンド。結果は `status` イベント） |
| `POST /clamd/stop` | A | `clamd_process` | clamd を停止する |
| `POST /diagnostics` | A | `DiagnosticsService.run()` | 診断を実行し、`mask_report` でマスクしたレポートを返す |

`GET /status` の応答例:

```json
{
  "clamd": {"reachable": true, "endpoint": "127.0.0.1:3310", "engine_version": "1.5.4", "database_version": "27801", "database_date": "..."},
  "realtime": {"running": true, "mode": "watchdog"},
  "scan": {"running": false, "job_id": null},
  "update": {"running": false, "last_executed_at": "...", "auto_update": true, "next_run": "..."},
  "schedule": {"next_run": "...", "next_job_name": "毎晩フルスキャン"},
  "quarantine": {"count": 3},
  "web": {"enabled": false}
}
```

### 6.2 スキャン

| メソッド・パス | ロール | 呼び出す処理 | 内容 |
|---|---|---|---|
| `POST /scans` | U | `ScanDispatcher.start()` | 開始。実行中なら 409 |
| `GET /scans/current` | U | — | 実行中（なければ直近）のジョブ |
| `POST /scans/current/cancel` | U | `ScanDispatcher.stop()` | 中止要求 |
| `GET /scans/history` | U | `ScanHistoryService.list()`（新設） | スキャン履歴。`?source=&since=&limit=&offset=` |
| `GET /scans/history/{id}` | U | `ScanHistoryService.get()` | 1件の詳細（対象、検知したファイルと検知名、エラーの内容） |

スキャン履歴（利用者の判断、12章 10）:

- 手動（トレイGUI・WebUI・`clamqtpie-ctl`）とスケジュールのすべてのスキャンを、終了時に `scan_history` テーブルへ記録する。clamav-webui の「スキャン状況・履歴」に相当する。
- 列: `id`、`started_at`、`finished_at`、`source`（`gui` | `web` | `ctl` | `schedule`）、`scan_type`（`quick` | `full` | `custom` | `schedule`）、`label`（スケジュールはジョブ名）、`targets`（JSON）、`scanned`、`detected`、`errors`、`result`（`completed` | `cancelled` | `failed`）、`detail`（失敗理由）。検知したファイルは `scan_history_detection`（`history_id`、`path`、`virus_name`、`action`（`quarantined` | `notified`））に持つ。
- Alembic `0005_add_scan_history` で追加する。保持期間は `logging.retention_days`。
- トレイGUI のスキャン画面と WebUI のスキャン画面に履歴の一覧を置く。終了時に `scan.finished` に `history_id` を付けて送る。

`POST /scans` の入力:

```json
{"type": "custom", "targets": ["D:\\Downloads"], "include_subfolders": true, "apply_exclude": true}
```

- `type`: `quick` | `full` | `custom`。
  - `full`: `scan_targets.full_scan_targets()`・`full_scan_skip_dirs()` をサーバー側で使う。
  - `quick`: サービスは SYSTEM／root で動くため `QStandardPaths` が利用者のフォルダを指さない。
    - トレイGUI からは **`targets` を GUI が求めて送る**（GUI は利用者権限で動くので従来の `quick_scan_targets()` がそのまま使える）。
    - `targets` を省略した場合（ブラウザ・`clamqtpie-ctl`）は、設定 `scan.quick_targets`（新設）に **利用者が指定した場所だけ** を使う（利用者の判断、12章 4）。既定は空で、空の間は 400 を返し、WebUI のクイックスキャンのボタンは無効にして設定画面への案内を出す。
    - スケジュールのジョブは従来どおり対象パスを明示するので、この設定の影響を受けない。
  - `custom`: `targets` 必須。絶対パスで存在するものに限る。
- `label` はサーバーが `type` から決める（`手動スキャン`／`クイックスキャン`／`フルスキャン`）。

### 6.3 隔離

| メソッド・パス | ロール | 呼び出す処理 | 内容 |
|---|---|---|---|
| `GET /quarantine` | U | `QuarantineService.list_all()` | 一覧（`restored`・`deleted` を含む） |
| `POST /quarantine/{id}/restore` | A | `restore(id, dest_path, overwrite)` | 入力 `{"dest_path": null, "overwrite": false}`。同名ファイルがあり `overwrite=false` なら 400（E3002） |
| `DELETE /quarantine/{id}` | A | `delete_permanently(id)` | 完全削除 |

- 復元は SYSTEM／root による任意の場所への書き込みになるため admin 限定。`dest_path` は絶対パスに限る。
- 復元したファイルが SYSTEM／root の所有にならないよう、隔離するときに元の所有者と権限を記録し、復元時に戻す（Windows はセキュリティ記述子を SDDL で、Linux は uid・gid・mode）。`quarantine` テーブルに `owner_info` 列を Alembic `0007_add_quarantine_owner` で追加する。記録のない旧データは、Windows は復元先フォルダの継承 ACL、Linux は復元先ディレクトリの所有者に合わせる。

### 6.4 除外

| メソッド・パス | ロール | 呼び出す処理 | 内容 |
|---|---|---|---|
| `GET /excludes` | U | `ExcludeService.list_all()` | |
| `POST /excludes` | A | `add(type, value, enabled)` | `type`: `file` | `folder` | `extension` | `process` |
| `PATCH /excludes/{id}` | A | `set_enabled()` | `{"enabled": false}` |
| `DELETE /excludes/{id}` | A | `remove()` | |

- 追加・変更後は従来どおり `realtime_manager.on_excludes_changed(type)` を呼ぶ。

### 6.5 リアルタイム監視

| メソッド・パス | ロール | 呼び出す処理 | 内容 |
|---|---|---|---|
| `GET /realtime` | U | `realtime_manager` | `{running, mode, default_action}` |
| `PUT /realtime` | A | `start()`／`stop()` | `{"enabled": true}`。開始に失敗したら 500 と理由 |
| `GET /realtime/targets` | U | `WatchTargetService.list_all()` | |
| `POST /realtime/targets` | A | `add(path)` → `realtime_manager.add_target()` | |
| `PATCH /realtime/targets/{id}` | A | `set_enabled()` | |
| `DELETE /realtime/targets/{id}` | A | `remove()` → `remove_target()` | |

### 6.6 スケジュール

| メソッド・パス | ロール | 呼び出す処理 | 内容 |
|---|---|---|---|
| `GET /schedules` | U | `SchedulerService.list_all()` | `ScheduleRecord` の一覧 |
| `POST /schedules` | A | `add_job(name, scan_type, cron, target, wake)` | `scan_type`: `scan` | `update`。`wake`: スリープから起こして実行するか（既定 false） |
| `PUT /schedules/{id}` | A | `update_job()` | |
| `PATCH /schedules/{id}` | A | `set_enabled()` | `{"enabled": false}` |
| `DELETE /schedules/{id}` | A | `remove_job()` | |
| `POST /schedules/{id}/run` | A | `run_now()` | 同じジョブを実行中なら 409 |

- 自動定義更新（`update.auto_update`／`interval_hours`）は設定（6.8）で変える。保存後は従来どおり `set_auto_update()` を呼ぶ。自動定義更新ではスリープから起こさない。
- `schedule` テーブルに `wake` 列を Alembic `0006_add_schedule_wake` で追加する。ジョブの編集ダイアログ（トレイGUI・WebUI）に「スリープ中でも起こして実行する」を置く。スリープからの復帰の仕組みは 9.1。

### 6.7 定義更新

| メソッド・パス | ロール | 呼び出す処理 | 内容 |
|---|---|---|---|
| `POST /updates` | U | `UpdateDispatcher.start()` | 開始（202）。実行中なら 409 |
| `GET /updates/current` | U | — | 実行中（なければ直近）のジョブ |
| `GET /updates/history` | U | `UpdateService.list_history()` | |

### 6.8 設定

| メソッド・パス | ロール | 呼び出す処理 | 内容 |
|---|---|---|---|
| `GET /config` | U | `ConfigService.current` | `ConfigModel.to_dict()`。SMTP パスワードは返さず `smtp.has_password` を付ける |
| `PUT /config` | A | `ConfigService.save()` | 全体を置き換える。保存後に従来の `_on_settings_saved` 相当の反映（通知の再構成、自動更新の再登録など）を行い `config.changed` を送る |
| `PUT /config/smtp-password` | A | `set_smtp_password()` | `{"password": "..."}` |
| `DELETE /config/smtp-password` | A | `delete_smtp_password()` | |
| `POST /notifications/test-mail` | A | `NotificationService.send_test_mail()` | |

設定の分割:

- `general` の `autostart`／`start_minimized`／`minimize_to_tray`／`show_tray_hint_shown`／`language` は **利用者ごとの GUI 設定** に移す（Windows: `%APPDATA%\ClamQtPie\gui.json`、Linux: `~/.config/clamqtpie/gui.json`）。サービスの設定には残さない。
- 新設: `api`（`port`、`web_enabled`、`web_port`、`web_lan_enabled`（初期値 false）、`web_bind_address`（空なら `0.0.0.0`）、`web_allowed_networks`（空ならプライベートアドレス全体）、`web_allowed_hosts`、`web_username`（初期値 `admin`）、`web_password_hash`（初期値は空＝未設定）、`web_password_length`（LAN 許可時の長さ確認用。ハッシュからは長さが分からないため設定時に記録）、`session_timeout_min`）、`scan`（`quick_targets`）、`schedule`（`misfire_grace_sec`）。`schema_version` を 2 に上げ、`legacy_migration` に移行処理を追加する。
- SMTP パスワードの保存先は keyring から変える。SYSTEM の資格情報ストアは利用者のものと別で、Linux の root には Secret Service がないため。Windows: DPAPI（`CRYPTPROTECT_LOCAL_MACHINE`）で暗号化して設定ディレクトリのファイルに保存。Linux: 0600 のファイル。
- 除外の種類は従来の `file`／`folder`／`extension`／`process` のまま。clamav-webui のシグネチャ名の除外（`local.ign2`）は取り込まない（利用者の判断、12章 9）。

### 6.9 ログ

| メソッド・パス | ロール | 呼び出す処理 | 内容 |
|---|---|---|---|
| `GET /logs` | U | `LogReaderService.read_entries()`・`filter_entries()` | `?since=&category=&level=&q=&limit=&offset=` |
| `GET /logs/export.csv` | U | `export_csv()` | CSV ファイルを返す |
| `DELETE /logs` | A | `clear()` | |

### 6.10 通知（デスクトップ表示用）

| メソッド・パス | ロール | 内容 |
|---|---|---|
| `GET /notifications/pending` | U | 未読の通知（7章）。トレイGUI・WebUI の両方から使う |
| `POST /notifications/ack` | U | `{"ids": [1, 2]}` または `{"all": true}` で既読にし、`notification.acked` を送る |

### 6.11 WebUI・認証

| メソッド・パス | ロール | 内容 |
|---|---|---|
| `GET /web` | U | `{"enabled": bool, "lan": bool, "urls": ["http://127.0.0.1:8766/web/login", "http://192.168.1.10:8766/web/login"], "username": "admin", "password_set": bool}` |
| `PUT /web` | A（**Bearer のみ**） | `{"enabled": true, "lan": false}`（どちらか片方でも可）。`api.web_enabled`・`api.web_lan_enabled` を保存し、WebUI の待ち受けを起動・停止・再起動する。パスワード未設定なら 400、LAN 許可でパスワードが 12 文字未満なら 400 |
| `PUT /web/credentials` | A（**Bearer のみ**） | `{"username": "admin", "password": "..."}`。ログインアカウントを設定し、既存のセッションを破棄する |
| `POST /auth/session` | なし | `{"username": "...", "password": "..."}` で Cookie セッションを発行する。WebUI OFF なら 404、ロック中は 429 |
| `DELETE /auth/session` | Cookie | ログアウト |

------------------------------------------------------------------------

## 7. 通知（トレイGUI・WebUI 共通の未読管理）

### 7.1 サービス側

- `NotificationService._send_desktop_notification()` は、サービスでは直接表示せず、**`pending_notification` テーブルに未読として保存して `notification` イベントを送る** 形に置き換える（メール送信はサービスで従来どおり）。
- 未読・既読の状態はサービスが1か所で持つ。トレイGUI と WebUI のどちらで既読にしても同じ状態が更新され、`notification.acked` イベントでもう片方の表示からも消える。
- `pending_notification` は Alembic `0004_add_pending_notification` で追加する（`id`、`created_at`、`title`、`body`、`open_screen`、`acked_at`）。既読のものは `logging.retention_days` を過ぎたら削除する。

### 7.2 トレイGUI

- 「トーストを表示したこと」と「既読」は分ける。トーストを表示しただけでは既読にしない（既読にすると WebUI に出なくなるため）。表示済みの通知 ID は GUI がメモリに持ち、同じ通知を2回表示しない。
- 既読にするのは、利用者がトーストをクリックしたとき、またはホーム画面の通知一覧で「既読にする」「すべて既読」を押したとき。
- WebUI 側で既読にされたら（`notification.acked`）、通知センターに残っているトーストも消す。
  - Windows: トーストに `Tag` = 通知 ID を付けて表示し、`ToastNotificationManager.History.Remove(tag, group, APP_ID)` で消す。
  - Linux: `notify-send --print-id` で受け取った ID を覚えておき、`org.freedesktop.Notifications.CloseNotification` で閉じる。ID が取れない環境では消さない（表示が残るだけで、未読の状態は正しい）。
- GUI の起動時（ログイン時）は `GET /notifications/pending` で、ログインしていない間に溜まった未読の通知をまとめて1件のトーストにして表示する。
- トーストのクリックで開く画面（`open_screen`）は従来の `ALLOWED_SCREENS` の範囲に限る。
- トーストを表示するのは管理者（`ClamQtPie Users`／`clamqtpie` グループの利用者）のトレイGUI だけ（3.2、利用者の判断）。複数の管理者がログインしている場合は、それぞれの GUI が表示し、誰かが既読にした時点で全員の表示から消える。

### 7.3 WebUI

- WebUI が起動していてブラウザでログインしている間は、ブラウザでも通知を確認して消せる。
- 画面上部に通知のアイコンと未読件数を常に表示する。クリックすると未読の一覧（日時、タイトル、本文、関連画面へのリンク）を開き、1件ずつの「既読にする」と「すべて既読」で消せる（`POST /notifications/ack`）。
- 新しい通知は SSE の `notification` で受け取り、件数と一覧をすぐ更新する。画面の右下に数秒間、ページ内の通知も出す。
- ブラウザの OS 通知（Notification API）は、ブラウザの仕様で HTTPS か `localhost` でしか使えない。LAN から HTTP で開いている場合はページ内の表示だけにする。HTTPS 対応（3.5）後は、CA 証明書を登録した端末で OS 通知も使えるようにする（利用者が設定画面で許可した場合のみ）。
- GUI のない Linux では WebUI が唯一の通知の確認場所になる。そのため「未読が溜まり続ける」問題もこれで解消する（12章 13）。

------------------------------------------------------------------------

## 8. スレッドモデル

```text
uvicorn スレッド（asyncio ループ）         メインスレッド（QCoreApplication）
  async def handler():
    fut = invoker.call(fn, *args)  ──────▶  QtInvoker（QObject、Signal 経由で受け取る）
    return await asyncio.wrap_future(fut)    fn(*args) を実行し concurrent.futures.Future に結果を設定
                                   ◀──────
  EventHub.subscribe() → asyncio.Queue  ◀── Qt Signal → EventHub.publish()
                                             （loop.call_soon_threadsafe で各キューへ）
```

- **状態を変える処理と QObject を触る処理はすべて `QtInvoker` でメインスレッドに渡す**（`ScanDispatcher.start/stop`、`realtime_manager.start/stop/add_target`、`UpdateDispatcher.start`、設定の保存と反映）。実行中かどうかの判定（409）もメインスレッドで行い、GUI・ブラウザ・スケジュールの同時操作の取り合いを防ぐ。
- DB の読み取りだけの処理（各 `list_all()`、`list_history()`、ログ読み取り）は FastAPI のスレッドプールで直接呼んでよい。`DatabaseService.session()` は呼び出しごとにセッションを作る。
- SQLite の扱い（12章 6）:
  - 今もスケジュール（APScheduler のスレッド）やリアルタイム監視のスレッドから DB を使っており、複数のスレッドからの利用は既に行っている。SQLAlchemy 2.0 はファイルの SQLite に対して接続プールを使い、`check_same_thread` を自動で外す。実装時にこれをテストで確認する。
  - 接続時に `PRAGMA journal_mode=WAL` と `PRAGMA busy_timeout=5000` を設定する。読み取りが書き込みを待たなくなり、書き込みどうしが重なったときも 5 秒までは待つ。
  - 書き込みは短い処理（1行の追加・更新）だけなので、アプリ側で書き込みを1本にまとめる仕組みは作らない。
- スケジュールの発火（APScheduler のスレッド）は従来の `_GuiBridge` 相当でメインスレッドへ渡す。この部分は変えない。

------------------------------------------------------------------------

## 9. サービス本体

| 項目 | Windows | Linux |
|---|---|---|
| 入口 | `service_main.py`（pywin32 `win32serviceutil.ServiceFramework`。pywin32 は既存の依存） | `service_main.py`（systemd から直接起動、`Type=notify`） |
| 起動完了の通知（12章 7） | 内部 API が待ち受けを始めてから `SERVICE_RUNNING` を報告する（それまでは `SERVICE_START_PENDING`） | 内部 API が待ち受けを始めてから `sd_notify("READY=1")` を送る。`NOTIFY_SOCKET` への UNIX データグラム送信を自前で実装し、依存は増やさない。停止時は `STOPPING=1` |
| 名前・権限 | サービス `ClamQtPie`、LocalSystem、自動起動（遅延開始） | `clamqtpie.service`、root |
| 依存関係 | clamd サービスに依存（`sc config ClamQtPie depend= clamd`） | `After=clamav-daemon.service`、`Wants=clamav-daemon.service` |
| データ | `%ProgramData%\ClamQtPie\`（config.json、app.db、logs、quarantine、api） | 設定 `/etc/clamqtpie/`、データ `/var/lib/clamqtpie/`、ログ `/var/log/clamqtpie/` |
| 二重起動防止 | `SingleInstance` をサービス側に移す | 同左 |
| 権限昇格 | 不要 | 不要（`pkexec` の経路は `_is_root()` 判定により通らない） |

- 起動順は従来の `app.py` の10ステップから画面に関わるもの（ウィンドウ・トレイ・画面のバインド）を除いたもの＋API の起動。
- スケジュールの取りこぼし対策: `BackgroundScheduler(job_defaults={"coalesce": True, "misfire_grace_time": <schedule.misfire_grace_sec、既定 3600>})`。加えて起動時と、スリープからの復帰時に、各ジョブの `last_run` と cron を比べ、電源オフ・スリープ中に過ぎた回があれば1回だけ実行する（`wake` が false のジョブも、復帰後に取り返す）。
- Linux の定義更新（CLAUDE.md の「`pkexec freshclam`／`skipped`」の分岐）: サービスは root なので `pkexec` は使わない。`clamav-freshclam.service` が動いている場合、無人実行（自動・スケジュール）は従来どおり `skipped`。利用者の操作（GUI・WebUI の「今すぐ更新」）は、freshclam を直接実行するとデーモンとロックがぶつかるため、`systemctl restart clamav-freshclam.service` で即時の確認を促し、その結果を `journalctl -u clamav-freshclam` から読んで更新履歴に記録する。動いていない場合は freshclam を直接実行する。

### 9.1 スリープからの復帰（利用者の判断、12章 8）

- `wake` が true のジョブのうち、次の実行時刻が最も早いもの1つに合わせて、OS の復帰タイマーを1つだけ設定する。ジョブの追加・変更・削除・有効無効の切り替えと、ジョブの実行後に設定し直す。
- 復帰時刻は実行時刻の 2 分前にする（ネットワークや clamd の準備を待つため）。
- Windows: サービス内で `win32event.CreateWaitableTimer` と `SetWaitableTimer(..., fResume=True)` を使う。復帰タイマーは電源オプションの「スリープ解除タイマーの許可」が有効な場合にだけ働く（多くの PC でバッテリー駆動時は無効）。`clamqtpie-ctl status` と診断画面でこの設定を確認して表示する（`powercfg /waketimers` と電源プランの値）。
- Linux: `/etc/systemd/system/clamqtpie-wake.timer`（`OnCalendar=<次の時刻の絶対日時>`、`WakeSystem=true`、`AccuracySec=1s`）と、何もしない `clamqtpie-wake.service`（`ExecStart=/bin/true`）を書き、`systemctl daemon-reload` して有効にする。`wake` が true のジョブがなくなったらタイマーを止めて削除する。
- 復帰したあとの再スリープは OS のアイドル設定に任せる。ジョブの実行中は、Windows は `SetThreadExecutionState(ES_SYSTEM_REQUIRED)`、Linux は `systemd-inhibit --what=sleep` でスリープを防ぎ、終わったら解除する（`wake` の有無にかかわらず、すべてのスケジュールジョブで行う）。
- ノート PC で鞄の中などで起動してしまうことがあるため、`wake` はジョブごとに選べるようにし、既定は false。

### 9.2 既存の利用者のデータの移行（12章 11）

- 移行は `clamqtpie-ctl migrate --from <旧フォルダ>`（管理者／root で実行）で行う。Windows のインストーラは、旧版の自動起動の登録（`HKCU\...\Run` の `ClamQtPie`）から旧フォルダを見つけたら、インストールの最後にこのコマンドを実行するか尋ねる。
- 移すもの: `config.json`（`schema_version` 1 → 2 の変換と、`general` の GUI 用の項目を実行した利用者の `gui.json` へ分離）、`app.db`（Alembic で最新まで適用）、`quarantine/`（中身ごと移動。DB は `uuid` で参照しているのでパスの書き換えは不要）、`logs/`。
- 隔離ファイルの移動は1件ずつ行い、途中で失敗したら移したものを元に戻して中止する。旧フォルダは削除せず、移行済みの印（`MIGRATED.txt`）を置く。
- SMTP パスワード: Windows は、UAC で昇格しても同じ利用者の資格情報ストアを読めるので、keyring から読み出して新しい保存先（DPAPI マシン単位）へ移す。Linux は `sudo` では利用者の Secret Service を読めないため、移行の最後にパスワードの再入力を求める（スキップ可。後で設定画面から入力できる）。
- 旧版の自動起動の登録と `clamqtpie:` URL スキームは、新しいトレイGUI の初回起動時に新しいものへ登録し直す（`legacy_migration` を拡張）。

### 9.3 配布・導入・削除（利用者の判断、12章 14〜17）

サービスは SYSTEM／root で動くため、その実行ファイルを一般ユーザーが書き換えられる場所に置くと、権限昇格の穴になる。そのため、ポータブル ZIP（インストーラ_パッケージング設計書 1章）をやめ、管理者しか書けない場所に導入する。

| 項目 | Windows | Linux |
|---|---|---|
| 配布物 | `ClamQtPie_Setup_v<ver>_win64.exe`（Inno Setup） | `ClamQtPie_v<ver>_linux_x86_64.tar.gz`（PyInstaller の出力＋`install.sh`・`uninstall.sh`） |
| 導入先 | `%ProgramFiles%\ClamQtPie\`（データは `%ProgramData%\ClamQtPie\`） | `/opt/clamqtpie/`（設定・データ・ログは 9章の表のとおり） |
| 導入で行うこと | ファイルの配置、`%ProgramData%\ClamQtPie\` の作成と ACL 設定（SYSTEM・Administrators はフル、`ClamQtPie Users` は `api\user.token` の読み取りのみ）、`ClamQtPie Users` グループの作成と導入した管理者の登録、サービスの登録（遅延自動起動・回復動作、9.4）と開始、`clamqtpie-ctl` を `PATH` に追加、旧版の検出と移行の確認（9.2）、スタートメニュー | `install.sh`（root で実行）: `/opt/clamqtpie/` への配置、`clamqtpie` グループの作成と `SUDO_USER` の登録、ディレクトリの作成と権限設定、`clamqtpie.service` の配置と `systemctl enable --now`、`/usr/local/bin/clamqtpie-ctl` へのリンク、`/usr/share/applications/` のデスクトップファイル |
| 更新（上書き導入） | サービスを止め、Restart Manager でトレイGUI を閉じてから差し替える。DB は起動時に Alembic で最新に上げる。完了後にサービスを開始し、トレイGUI を元の利用者の権限で起動し直す | `install.sh` を再実行する（同じ手順で、サービスの停止・差し替え・再開始） |
| 削除 | サービスの停止と削除、ファイル、グループ、`PATH`、ファイアウォール規則（3.1）、復帰タイマー（9.1）を消す | `uninstall.sh` で同じものを消す（`clamqtpie-wake.timer` を含む） |
| データの扱い（12章 17） | 「設定・履歴・ログ・隔離ファイルも削除する」の確認を出す（既定は残す）。消す場合は隔離ファイルも含めて `%ProgramData%\ClamQtPie\` を丸ごと削除 | `uninstall.sh` が同じことを尋ねる（既定は残す）。`--purge` で確認なしに削除 |

- PyInstaller の出力を3つに分ける。`ClamQtPieService.exe`（console=False、UAC なし）、`ClamQtPie.exe`（トレイGUI、**`uac_admin=False` に変更**）、`ClamQtPieElevate.exe`（`uac_admin=True`）。3つは同じ `_internal/` を共有する（`MERGE` ではなく、1つの spec で3つの `EXE` と1つの `COLLECT` を作る）。`clamqtpie-ctl` はコンソールの実行ファイルとして加える。
- Linux の配布物は、ビルドした環境より古い glibc の環境では動かない。ビルドは対応する最も古いディストリビューション（初版は Ubuntu 22.04 相当）で行い、対応範囲を README に書く。
- `build_release.py` は Windows では Inno Setup（`ISCC.exe`）を、Linux では tar.gz の作成を呼ぶ。インストーラ_パッケージング設計書を全面的に書き直す（段階 12-6）。

### 9.4 運用上の細目（こちらで決めた事項、12章 18〜21）

- **版数の確認:** トレイGUI は接続のたびに `GET /version` を呼び、API 版数が違えば接続をやめて E8107 を出す。アプリ版数だけが違う場合は警告を出したうえで使い続けられる。インストーラは両方を同時に入れ替えるので、通常は起きない（更新の途中や、手で差し替えたときだけ）。
- **サービスの回復動作:** Windows は失敗時に 60 秒後に再起動を2回まで（失敗回数は1日でリセット）。Linux は `Restart=on-failure`、`RestartSec=60`、`StartLimitIntervalSec=1d`、`StartLimitBurst=3`。
- **ポートの競合:** 内部 API（`api.port`）の待ち受けに失敗したら、E8100 をログに残してサービスの起動を失敗させる（API がなければトレイGUI も WebUI も使えないため）。サービスは待ち受けたポート番号を `api\endpoint.json`（トークンと同じ権限）に書き、トレイGUI・`clamqtpie-ctl` はそれを読んで接続する。ポートは `clamqtpie-ctl config set api.port <n>` で変えられる。WebUI の待ち受け（`api.web_port`）の失敗では、サービスは止めずに WebUI を OFF にして通知する。
- **復元したファイルの所有者:** 6.3 のとおり、隔離時に記録した所有者と権限に戻す。

------------------------------------------------------------------------

## 10. トレイGUI の変更

- `gui/` から `core/` のサービスを直接作らない。`api_client/` を新設する。
  - `ApiClient`: HTTP 通信、トークンの付与、エラー JSON から `AppError` への変換。Qt の `QNetworkAccessManager` を使い、画面のスレッドを止めない。
  - サービスごとの **プロキシ**（`QuarantineProxy.list_all()` など）は、既存サービスと同じメソッド名・戻り値の型にそろえる。各画面の `bind_services(...)` に渡すものを差し替えるだけで済む部分を増やすため。
  - `EventStream`: SSE を受信して Qt Signal に変換する。既存の `ScanDispatcher.progress` などと同じシグネチャにする。
- サービスに接続できないとき（未導入・停止中）は、E8105 を表示し「サービスの状態を確認」の案内を出す。トレイアイコンはエラー状態にする。
- admin が必要な操作で 403（E8102）を受けたら、昇格ヘルパーで admin トークンを取得して1回だけ再試行する。
- トレイメニューに「WebUI」のサブメニューを追加し、WebUI 関連の3項目をその中にまとめる。

```text
メイン画面を開く
今すぐスキャン
定義更新
リアルタイム監視 ON/OFF
WebUI ▶ ┬ WebUI ON/OFF          （チェック付き、PUT /web。パスワード未設定なら先にログイン設定を開く）
        ├ LAN からの接続を許可   （チェック付き、PUT /web の lan。暗号化されない旨の確認を出す）
        ├ ログイン設定...        （ユーザー名・パスワード・確認入力のダイアログ、PUT /web/credentials）
        ├ ブラウザで開く         （既定ブラウザで /web/login を開く。WebUI OFF の間は無効表示）
        └ CA 証明書を保存...     （12-7 で追加。HTTPS がローカル CA のときだけ表示、3.5）
────────
終了
```
- 自動起動（HKCU Run／XDG autostart）はトレイGUI の起動用として残す。利用者ごとの設定のまま。
- `ClamQtPie Users`（`clamqtpie`）グループに入っていない利用者がトレイGUI を起動したときは、トークンを読めないので、グループへの追加方法（`clamqtpie-ctl users add <名前>`）を案内して終了する。

------------------------------------------------------------------------

## 11. ブラウザ WebUI

- clamav-webui から流用するもの: `index.html` の骨組みと CSS、`app.js` の fetch 処理と `data-action` → `ACTIONS` の仕組み、CSP（`script-src 'self'`）、`textContent` で表示する方針と `tests/test_security.py` 相当の検査。
- 画面: ホーム（状態）、スキャン、隔離、除外、リアルタイム監視、スケジュール、定義更新、ログ、設定、診断。GUI のない環境では WebUI が唯一の画面になるため、トレイGUI の機能はすべて WebUI でも使えるようにする（WebUI のログイン設定だけは 3.4 の `clamqtpie-ctl` で行う）。
- 進捗表示は SSE（`EventSource` は Cookie を送るので Cookie セッションで認証できる）。
- 画面上部に通知のアイコン（未読件数）を置き、通知の確認と既読をブラウザからも行えるようにする（7.3）。
- clamav-webui 側にしかない「シグネチャ名の除外（`local.ign2`）」は取り込まない（12章 9）。

------------------------------------------------------------------------

## 12. 決定事項の一覧（旧「未確定事項」）

Phase 12 の計画時に未確定だった事項と、その決定。番号は本文中の「12章 n」に対応する。

| # | 事項 | 決定 | 本文 |
|---|---|---|---|
| 1 | admin 操作の昇格方法 | 操作したときだけ UAC／pkexec で昇格し、admin トークンを 15 分保持する（利用者の判断） | 3.2 |
| 2 | WebUI のセッションと権限 | 固定アカウント1組で権限は admin。有効期限は 30 分（LAN 許可中は 15 分） | 3.3 |
| 3 | Linux の UNIX ソケット＋`SO_PEERCRED` | 使わない。Windows と同じ TCP＋トークンにそろえ、実装と試験を1通りにする | 3.1 |
| 4 | クイックスキャン対象の既定 | ブラウザ・`clamqtpie-ctl` からは設定で指定した場所だけ（既定は空）。トレイGUI からはその利用者のフォルダを送る（利用者の判断） | 6.2 |
| 5 | 複数の利用者がいるときの通知 | 管理者（`ClamQtPie Users`／`clamqtpie` グループ）だけに表示する。トレイGUI 自体も管理者だけが使える（利用者の判断） | 3.2・7.2 |
| 6 | SQLite の複数スレッドからの利用 | WAL と `busy_timeout=5000`。書き込みの直列化の仕組みは作らない | 8 |
| 7 | systemd の `Type=notify` | 対応する（依存なしの自前実装）。Windows も API の待ち受け開始後に RUNNING を報告する | 9 |
| 8 | スリープからの復帰 | ジョブごとに「起こして実行する」を選べる（既定 OFF）。Windows は待機タイマー、Linux は `WakeSystem=` のタイマー（利用者の判断） | 9.1 |
| 9 | シグネチャ名の除外 | 取り込まない（利用者の判断） | 6.8・11 |
| 10 | スキャン履歴 | DB（`scan_history`）に残し、トレイGUI・WebUI に一覧を出す（利用者の判断） | 6.2 |
| 11 | 既存の利用者のデータの移行 | `clamqtpie-ctl migrate --from`。SMTP パスワードは Windows は自動で移し、Linux は再入力 | 9.2 |
| 12 | WebUI の HTTPS 対応 | ローカル CA でサーバー証明書を発行する。初版は HTTP（パスワードのみ）、段階 12-7 で対応 | 3.5 |
| 13 | GUI のない Linux での通知 | WebUI に未読通知の一覧を出し、ブラウザから既読にできる | 7.3 |
| 14 | Windows の配布形式 | Inno Setup のインストーラ。`%ProgramFiles%` に導入し、サービス登録・グループ作成・旧版の移行確認まで行う（利用者の判断） | 9.3 |
| 15 | Linux の配布形式 | tar.gz＋`install.sh`／`uninstall.sh`。`/opt/clamqtpie/` に導入する（利用者の判断） | 9.3 |
| 16 | 従来の1プロセス・ポータブル形態 | 残さない。ポータブル利用はできなくなる（利用者の判断） | 1 |
| 17 | 削除時のデータ | 削除するか尋ね、既定は残す。消す場合は隔離ファイルも含めて削除（利用者の判断） | 9.3 |
| 18 | サービスとトレイGUI の版数の違い | API 版数が違えば接続しない（E8107）。アプリ版数だけの違いは警告 | 9.4 |
| 19 | サービスの回復動作 | 60 秒後に再起動、2回まで（1日でリセット） | 9.4 |
| 20 | ポートの競合 | 内部 API は起動失敗にする。実際のポートは `endpoint.json` で知らせる。WebUI は OFF にして通知 | 9.4 |
| 21 | 復元したファイルの所有者 | 隔離時に記録した所有者・権限に戻す（`0007_add_quarantine_owner`） | 6.3 |

実装時に実機で確かめること:

- CA 証明書を登録した各ブラウザ・OS（Windows、macOS、iOS、Android、Linux の Firefox・Chrome）で、警告なしに開け、通知が使えること。
- Windows の待機タイマーで SYSTEM のサービスからスリープを解除できること（AC 電源・バッテリー駆動の両方、電源プランの設定ごと）。Linux の `WakeSystem=` が対象機で働くこと。
- UAC で昇格していない管理者が、`ClamQtPie Users` グループ経由で `user.token` を読めること。
- SQLAlchemy 2.0 で、別スレッドからの DB 利用に `check_same_thread` の追加設定が要らないこと。
- Inno Setup の Restart Manager でトレイGUI を閉じ、更新後に元の利用者の権限で起動し直せること。
- Linux の tar.gz が、ビルド環境より新しい対象ディストリビューション（Ubuntu・Debian・Fedora など）で動くこと。

------------------------------------------------------------------------

## 13. 実装の段階（案）

| 段階 | 内容 | 終わった時点で動くもの |
|---|---|---|
| 12-1 | `api/` の骨組み（FastAPI、uvicorn スレッド、`QtInvoker`、`EventHub`、認証、エラー変換）。読み取り API（`/version`、`/status`、各一覧、ログ） | 従来の `app.py` の中で API が動き、curl で読める |
| 12-2 | 状態を変える API（隔離、除外、監視対象、スケジュール、設定、リアルタイム監視の ON/OFF）、ジョブ型 API（スキャン、定義更新）、SSE、スキャン履歴（`0005_add_scan_history`）、復元時の所有者の復帰（`0007_add_quarantine_owner`）、SQLite の WAL 化 | API だけで全機能を操作できる |
| 12-3 | `service_main.py`、Windows サービス・systemd unit（`Type=notify`）、データの置き場所、設定の分割、スケジュールの取りこぼし対策とスリープからの復帰（`0006_add_schedule_wake`）、通知の保存、Linux の定義更新の分岐の見直し、管理用コマンド `clamqtpie-ctl`（`web`・`status`・`users`・`config`・`migrate`）、回復動作・`endpoint.json` | サービスとして動き、ログインしなくてもスケジュールが実行される。GUI のない環境で WebUI のログインを設定できる |
| 12-4 | トレイGUI の API クライアント化（`api_client/`、プロキシ、`EventStream`、昇格ヘルパー、`ClamQtPie Users` グループの確認）、PyInstaller の3分割、版数の確認、Windows のインストーラ（Inno Setup）と Linux の `install.sh`／`uninstall.sh`（グループ作成、旧版からの移行、削除時のデータの確認） | GUI が一般権限で動き、サービスを操作する。インストーラで導入・更新・削除できる |
| 12-5 | ブラウザ WebUI（clamav-webui の画面を流用）、固定アカウントでのログイン、WebUI 用の待ち受けの分離、LAN からの接続許可（接続元 IP・Host 検査、ファイアウォール規則）、トレイの WebUI サブメニュー | ブラウザから操作できる。LAN の別の PC からも使える |
| 12-6 | 設計書（基本設計・詳細設計・画面設計・タスクトレイ常駐アプリプロセス設計・インストーラ・エラーコード・テスト仕様）の改訂、README_DEV への記録 | |
| 12-7 | WebUI の HTTPS 対応（ローカル CA、サーバー証明書の自動更新、CA 証明書の配布、`clamqtpie-ctl web tls`、ブラウザの OS 通知） | LAN から HTTPS で、警告なしに使える |

- 12-1・12-2 は従来の1プロセス形態のまま進められるため、途中でもアプリは動く状態を保てる。
