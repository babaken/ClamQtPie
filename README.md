<p align="center">
  <img src="clamav_frontend/icons/app.png" width="128" alt="ClamQtPie アイコン">
</p>

<h1 align="center">ClamQtPie</h1>

<p align="center">
  ClamAV（clamd / freshclam）を操作する Windows / Linux 向け GUI フロントエンド
</p>

<p align="center">
  日本語 | <a href="README_en.md">English</a>
</p>

---

> [!IMPORTANT]
> **v0.10.0 から、導入の方法が変わりました。** これまでのポータブル版（Zip を展開して管理者権限で起動する形）をやめ、Windows はインストーラ、Linux は `install.sh` で導入します。保護の処理は OS のサービスとして動き、タスクトレイのアプリは一般の権限で動きます。v0.9.x を使っている場合は、[ポータブル版（v0.9.x）からの移行](#ポータブル版v09xからの移行) の手順で設定・隔離したファイルなどを移してください。

## 概要

ClamQtPie は、オープンソースのウイルス対策エンジン [ClamAV](https://www.clamav.net/) を、タスクトレイに常駐するデスクトップアプリやブラウザから使えるようにする GUI フロントエンドです。Python と PySide6（Qt）で作られており、Windows と Linux の両方で動作します。

ClamAV エンジン自体は同梱しません。別途インストールした clamd と通信して動作します。

## 主な機能

| 機能 | 内容 |
|---|---|
| オンデマンドスキャン | フォルダ・ファイルを指定して検査。進捗と現在のファイルをリアルタイム表示 |
| リアルタイム監視 | Windows: watchdog でファイルの作成・変更を検知して検査<br>Linux: ClamAV 公式の **clamonacc**（オンアクセススキャン）と連携 |
| 隔離 | 検知ファイルを隔離フォルダへ移動。一覧・復元・完全削除 |
| スケジュール | cron 形式でスキャン・定義更新を定期実行（同じジョブの重複実行は防止。スリープ中の PC を起こして実行することも可能） |
| 定義更新 | freshclam による手動更新・自動更新と、更新履歴の表示。Linux では OS の定義更新サービス（clamav-freshclam）の開始・停止も画面から |
| 通知 | デスクトップ通知（Windows: Toast / Linux: notify-send）とメール通知（SMTP）。Windows では検知の通知をクリックすると隔離画面が開く。サービスが出した通知は、トレイのアプリを起動していなかった間の分も後から表示 |
| 除外設定 | ファイル・フォルダ・拡張子・プロセス単位で除外 |
| タスクトレイ常駐 | 状態別アイコン（通常 / 監視中 / 検知あり / エラー）、多重起動防止、サインイン時の自動起動 |
| WebUI | ブラウザからの操作（既定では OFF。ログイン・二段階認証・時間を決めて自動で OFF）。HTTPS（ClamQtPie のローカル CA）と、LAN の別の端末からの接続（試験的）にも対応 |
| ログ | 検知・スキャン・隔離・更新などをカテゴリ付きで記録。フィルタ・CSV 出力。エラーコード（例: `E1001`）から該当ログへ移動 |
| clamd の起動 | clamd が停止していれば OS のサービスとして起動を試みる（「ClamAV エンジン サービス」画面の「サービスを開始」ボタン、停止を検知したときに1回自動。この画面から停止した場合は自動では起動し直さない） |
| 管理用コマンド | `clamqtpie-ctl`: サービスの状態、設定（保護された項目を含む）、隔離、トレイのアプリを使える利用者、旧版からの移行、WebUI の設定 |

## 構成

ClamQtPie は、次の 4 つで動きます。

| 部分 | 内容 |
|---|---|
| サービス | 保護の処理（スキャン・リアルタイム監視・隔離・スケジュール・定義更新・通知）をすべて行う本体。Windows はサービス `ClamQtPie`（LocalSystem）、Linux は systemd の `clamqtpie.service`（root）。PC の起動時に自動で起動し、だれもサインインしていなくても動きます |
| トレイGUI | タスクトレイに常駐する画面（`ClamQtPie.exe` / `clamqtpie`）。一般の利用者の権限で動き、サービスに操作を頼みます。トレイGUI を閉じても、保護は止まりません |
| WebUI | ブラウザで開く画面。サービスが待ち受けます（既定では OFF） |
| clamqtpie-ctl | 管理用のコマンド。管理者（Windows）・root（Linux）で実行します |

トレイGUI は、ClamQtPie Users グループ（Windows）・`clamqtpie` グループ（Linux）に入っている利用者だけが使えます。導入した利用者は自動で入ります。

設定・データの置き場所:

| | Windows | Linux |
|---|---|---|
| 設定 | `%ProgramData%\ClamQtPie\config.json` | `/etc/clamqtpie/config.json` |
| DB・隔離したファイルなど | `%ProgramData%\ClamQtPie\` | `/var/lib/clamqtpie/` |
| ログ | `%ProgramData%\ClamQtPie\logs\` | `/var/log/clamqtpie/` |
| プログラム | `%ProgramFiles%\ClamQtPie\` | `/opt/clamqtpie/` |

## 動作要件

- **OS**: Windows 10 / 11（64bit）、または systemd を使う Linux
- **ClamAV**: 別途インストールが必要です（下記「必要な ClamAV コンポーネント」）
- **開発時**: Python 3.14 以降（3.15 でも動作確認済み）

### 必要な ClamAV コンポーネント

| コンポーネント | Windows | Linux | 用途 |
|---|---|---|---|
| clamd（検査エンジン） | **必須**。OS のサービス `clamd` として登録 | **必須**。`clamav-daemon` サービス | すべての検査（スキャン・リアルタイム監視）を clamd に依頼します |
| freshclam（定義更新） | **必須**。ClamQtPie のサービスが直接実行します | **必須**。ClamQtPie のサービスが直接実行します。OS の `clamav-freshclam` サービスを使ってもかまいません（下記「定義更新の動作」） | ウイルス定義の更新 |
| clamonacc | 使いません | リアルタイム監視を clamonacc 方式で使う場合 | 下記「Linux のリアルタイム監視（clamonacc）の準備」 |
| clamscan / clamdscan | 使いません | 使いません | 障害を調べるときにだけ使います |

- clamd が停止していると検査はできません（画面・トレイ・定義更新・隔離の管理・ログは使えます）。停止を検知すると、ClamQtPie が OS のサービスとして起動を1回試みます
- ClamQtPie は freshclam と clamonacc を **PATH**（サービスから見える、PC 全体の PATH）から探します。PATH に無い場合は、`clamqtpie-ctl config set update.freshclam_path <フルパス>`・`clamqtpie-ctl config set realtime.clamonacc_path <フルパス>` で指定してください（トレイGUI の設定画面では、これらの項目は表示だけで変えられません）
- clamd の接続先の既定値は、Windows が TCP `127.0.0.1:3310`、Linux が UNIX ソケット `/var/run/clamav/clamd.ctl` です。`clamd.conf` で別の値（`TCPSocket` / `TCPAddr` / `LocalSocket`）にした場合は、ClamQtPie の設定（`clamd.tcp_host` / `clamd.tcp_port` / `clamd.unix_socket_path`）も `clamqtpie-ctl config set <項目> <値> --restart` で同じ値にしてください。Linux のソケットの場所はディストリビューションによって異なるため、`clamd.conf` の `LocalSocket` を確認してください
- clamd が同じ PC で動く構成を前提にしています

### Windows の準備

<!-- 要確認: ClamAV 1.5.4 のインストーラーがサービス登録・PATH 追加まで行うか、conf_examples の場所、clamd.exe --install-service の動作を実機で確認すること -->

1. [ClamAV 公式サイト](https://www.clamav.net/downloads) から Windows 版をインストールします
2. インストール先（例: `C:\Program Files\ClamAV`）の `conf_examples` にある `clamd.conf.sample` と `freshclam.conf.sample` を、インストール先へ `clamd.conf` / `freshclam.conf` という名前でコピーし、それぞれの `Example` の行を削除します
3. `clamd.conf` で、次のように TCP で待ち受ける設定にします

   ```
   TCPSocket 3310
   TCPAddr 127.0.0.1
   ```

4. 管理者のコマンドプロンプトでインストール先へ移動し、定義を取得してから、clamd をサービスとして登録・起動します

   ```bat
   freshclam.exe
   clamd.exe --install-service
   sc start clamd
   ```

5. インストール先を、システムの環境変数 PATH に追加します（または、導入後に `clamqtpie-ctl config set update.freshclam_path "C:\Program Files\ClamAV\freshclam.exe" --restart` で場所を指定します）

### Linux の準備

`apt install clamav clamav-daemon`（Debian / Ubuntu 系）、`pacman -S clamav`（Arch 系）などでインストールし、clamd のサービスを有効にします。

```sh
sudo systemctl enable --now clamav-daemon.service
```

サービス名はディストリビューションによって異なる場合があります。トレイGUI を X11 のデスクトップで使う場合は、Qt が必要とする `libxcb-cursor0`（Arch 系は `xcb-util-cursor`）も入れてください。Wayland のデスクトップでは不要です。管理者の操作の認証に polkit（pkexec）を使います。

### 準備の確認

導入した後、トレイGUI の「システム診断」画面で「ヘルスチェックを実行」を押し、[NG] や [注意] が無いことを確認します。[注意] には、PATH に無い実行ファイルや、`clamd.conf` の待ち受け先と ClamQtPie の接続先の食い違いが表示されます。

### 動作確認済みの環境

| 環境 | 内容 |
|---|---|
| Windows 11 Pro | ClamAV 1.5.4 |
| Linux（EndeavourOS、KDE Plasma 6 の Wayland、systemd） | ClamAV 1.5.4（clamonacc 含む） |
| Linux（WSL2 Ubuntu 26.04） | 自動テストのみ |

## 導入

### Windows

1. リリースの `ClamQtPie-<版>-setup.exe` をダウンロードし、SHA-256 がリリースに書かれた値と同じことを確かめます（PowerShell で `Get-FileHash .\ClamQtPie-<版>-setup.exe`）
2. インストーラを実行します（管理者の権限が要ります）。選べる作業は次の 2 つです
   - サインインしたときにトレイGUI を起動する
   - 管理用コマンド（clamqtpie-ctl）を PATH に追加する
3. 完了の画面で再起動を勧められたら、再起動するか、サインアウトしてサインインし直してください。導入した利用者を ClamQtPie Users グループに新しく入れたため、サインインし直すまでトレイGUI がサービスにつながりません（それまでトレイGUI は起動しません）。更新のときは出ません

サービスは PC の起動時に自動で起動します。トレイGUI はスタート メニューの「ClamQtPie」からも起動できます。

### Linux

1. リリースの `ClamQtPie-<版>-linux64.tar.gz` をダウンロードし、SHA-256 を確かめてから展開します

   ```sh
   sha256sum -c ClamQtPie-<版>-linux64.tar.gz.sha256
   tar -xzf ClamQtPie-<版>-linux64.tar.gz
   ```

2. 展開したフォルダの `install.sh` を、トレイGUI を使う利用者から `sudo` で実行します

   ```sh
   sudo sh <展開したフォルダ>/install.sh
   ```

   `/opt/clamqtpie` に置き、サービスを登録して開始し、`sudo` を実行した利用者を `clamqtpie` グループに入れます。`clamqtpie-ctl` は `/usr/local/bin` に置きます
3. サインアウトしてサインインし直します（グループを有効にするため。使えなければ PC を再起動してください）
4. アプリのメニューの「ClamQtPie」からトレイGUI を起動します

### ほかの利用者にトレイGUI を使わせる

```sh
sudo clamqtpie-ctl users add <利用者名>      # Linux
clamqtpie-ctl users add <利用者名>           # Windows（管理者のコマンドプロンプト・PowerShell）
```

その利用者がサインインし直すと使えます。`users list` で一覧、`users remove` で外します。

## ポータブル版（v0.9.x）からの移行

v0.9.x（ポータブル版）の設定・隔離したファイル・スケジュール・除外・監視するフォルダ・ログを、サービスへ移せます。**旧版のログの日付を保つため、移行は導入した日のうちに行ってください。**

1. 旧版の ClamQtPie を終了します（タスクトレイのアイコンを右クリックして「終了」）
2. 新しい版を導入します（上の「導入」）
3. 旧版のデータを移します
   - **Windows**: インストーラが旧版（自動起動に登録された、導入先以外の `ClamQtPie.exe`）を見つけると、導入の後に移すかを尋ねます。「はい」で移します。見つからなかったときや後から移すときは、管理者のコマンドプロンプトで次を実行します

     ```bat
     clamqtpie-ctl migrate --from "<旧版のフォルダ>" --restart
     ```

   - **Linux**: 次を実行します

     ```sh
     sudo clamqtpie-ctl migrate --from <旧版のフォルダ> --restart
     ```

   移すもの（除外・監視するフォルダ・スケジュール・隔離中のファイル）の一覧を表示して確認を求めます。移行先にすでにデータがあるときや、旧版が動いているときは移しません
4. 動作を確かめてから、旧版のフォルダを削除します（移した後、旧版のフォルダには `MIGRATED.txt` が置かれます）

注意:

- 設定は、トレイGUI・WebUI から変えられる項目だけを移します。保護された項目（clamd の接続先・freshclam や clamonacc の場所など）は移さず、旧版の値が既定値と違えば最後に一覧を表示します。必要なら `clamqtpie-ctl config set` で設定してください
- SMTP パスワードは、Windows では自動で移します。Linux では入力を求めます（空にすると移しません）
- 旧版から移した隔離ファイルを復元すると、ファイルの所有者が管理者・root になります（元の所有者の記録が無いため）。復元の後に、所有者とアクセス権を必要に応じて直してください
- 旧版の自動起動の登録は、新しい版のトレイGUI が起動したときに、新しい版のトレイGUI に置き換えます

## 使い方

### トレイGUI

タスクトレイのアイコンを右クリックするとメニュー、ダブルクリックするとメイン画面が開きます。メイン画面の各画面（ホーム・スキャン・リアルタイム保護・隔離・スケジュール・セキュリティ インテリジェンス（定義更新）・保護の履歴（ログ）・設定・システム診断・ClamAV エンジン サービス）は、v0.9.x と同じように使えます。

### 管理者の操作

保護の設定を変える操作（リアルタイム監視の開始・停止、監視するフォルダ・除外・スケジュール・設定の変更、隔離ファイルの復元・削除、clamd の開始・停止など）は、管理者の確認を経てから行います。

- **Windows**: UAC の確認画面の後に、ClamQtPie の確認画面（何を変えるかの一覧）が出ます。「実行」で行います
- **Linux**: polkit の認証画面（管理者のパスワード）の後に、ClamQtPie の確認画面が出ます

リアルタイム保護・スケジュールの画面の変更は、画面の中にためておき、［変更を保存］でまとめて 1 回の確認で保存します。保存する前に画面を離れると、保存するかを尋ねます。

自分のフォルダ（ホームフォルダ）の中のスキャン・隔離・定義の更新・通知の既読は、確認なしでできます。

### 設定の保護された項目

clamd の接続先・サービス名・freshclam や clamonacc の場所などは、乗っ取りに使われないよう、トレイGUI・WebUI からは変えられません（設定画面には表示だけします。入力欄のツールチップにコマンドを示します）。管理用コマンドで変えます。

```sh
clamqtpie-ctl config show
clamqtpie-ctl config set <項目> <値> --restart     # --restart でサービスを再起動して反映
```

Windows は管理者のコマンドプロンプト・PowerShell で、Linux は `sudo` を付けて実行します。

### 定義更新の動作

- 「今すぐ更新」（定義更新の画面・ホーム・トレイ）、自動更新、スケジュールの定義更新は、ClamQtPie のサービスが freshclam を実行します
- **Linux で OS の定義更新サービス（clamav-freshclam）が動いているとき**は、同じ定義を重ねて更新しないよう、ClamQtPie の自動更新・スケジュールの定義更新は行わず、OS のサービスに任せます（記録するのは、見送りが始まったときだけです）。定義更新の画面の「OS の定義更新サービス (clamav-freshclam)」の欄で、状態と、どちらが定義を更新しているかが分かります
  - 「■ 停止」は、今止めて、PC の起動時にも起動しないようにします（`systemctl disable --now`）。以後は ClamQtPie が自動更新します
  - 「▶ 開始」は、今動かして、PC の起動時にも起動するようにします（`systemctl enable --now`）
  - どちらも管理者の操作です。clamav-freshclam が無い環境（Windows など）では、この欄は出ません

## WebUI

ブラウザから、トレイGUI とほぼ同じ操作ができます。既定では OFF で、ON の間だけサービスが待ち受けます。

### 使い始める

1. トレイのメニューの「WebUI」→「ログイン設定...」で、ユーザー名とパスワード（8 文字以上）を設定します。自動で作ることもできます（20 文字。1 回だけ表示します）
2. 「WebUI」→「ON にする」で、使う時間（1 時間・8 時間・24 時間・時間を指定...・OFF にするまで）を選びます。決めた時刻になると自動で OFF になります
3. 「WebUI」→「ブラウザで開く」で開き、ログインします

管理用コマンドでも操作できます（`clamqtpie-ctl web status`・`web enable --for 8h`・`web enable --until 23:00`・`web disable`・`web set-login` など。`clamqtpie-ctl web --help` を参照）。

- ログインしてから操作しない時間が 30 分続くと、ログアウトします
- 同じ接続元から 5 回続けてログインに失敗すると、5 分間ログインできません
- 管理者の操作は、確認のダイアログでパスワードを入れ直してから行います（入れ直してから 5 分間は省きます）
- 通知（未読の件数・一覧・既読）も WebUI から扱えます

### 二段階認証

認証アプリ（Google Authenticator・Microsoft Authenticator など、TOTP に対応したもの）の 6 桁の確認コードを、ログインのときにも求めます。

1. トレイのメニューの「WebUI」→「二段階認証の設定...」を開きます（または `clamqtpie-ctl web totp setup`）
2. 表示された QR コードを認証アプリで読み取り、表示された確認コードを入れます。認証アプリには `ClamQtPie:<この PC の名前>` の名前で登録されます
3. **予備のコード**が 1 回だけ表示されます。スマートフォンをなくしたときに、確認コードの代わりに 1 個ずつ使えます。控えておいてください

- 予備のコードを使うと、残りの数が表示されます。少なくなったら、二段階認証を設定し直してください（新しい予備のコードが表示されます）。WebUI からは設定し直せません
- パスワードは合っていて確認コードだけを 10 回間違えると、パスワードが漏れているおそれがあるため、パスワードと確認コードでのログインを止めて通知します（予備のコードでのログインはできます）。パスワードを変えるか、トレイのメニューの「WebUI」→「ログインの停止を解く」（`clamqtpie-ctl web unlock`）で解きます
- 解除は「二段階認証の設定...」→「解除する」（`clamqtpie-ctl web totp reset`）

### HTTPS（ClamQtPie のローカル CA）

このPC のブラウザから使うだけなら、HTTP のままで問題ありません（ブラウザは `127.0.0.1` への接続を安全なものとして扱います）。LAN の別の端末から使うときは HTTPS が必要です。

```sh
clamqtpie-ctl web tls on        # HTTPS を有効にする（切り替えた後の URL も表示します）
clamqtpie-ctl web tls status    # CA 証明書の SHA-256 のフィンガープリント・期限を表示する
```

ClamQtPie が、この PC だけの認証局（ローカル CA）と、それが発行したサーバー証明書を作ります。ブラウザを使う端末（この PC を含む）に CA 証明書を登録すると、警告なしで開けます。

CA 証明書の取り出し方は 2 つあります。

- **A（おすすめ）: PC で書き出して、ファイルで渡す。** ClamQtPie を動かしている PC で `clamqtpie-ctl web tls export-ca clamqtpie-ca.crt` を実行し（Windows は管理者、Linux は `sudo`）、できたファイルを USB メモリなどで端末に渡します。サービスのデータから直接書き出すので、この PC で登録するときは照合が要りません。別の端末では、ファイルを途中で取り替えられていないか、表示された SHA-256 のフィンガープリントを控えておき、登録した後に端末の証明書の詳細（表示できるとき）と見比べてください
- **B: 端末のブラウザでダウンロードする。** 端末のブラウザで WebUI を開き、警告を越えてログイン画面を開いて、「CA 証明書をダウンロード」で保存します。ログイン画面の SHA-256 のフィンガープリントが、PC の `clamqtpie-ctl web tls status` の表示と同じことを確かめてから登録します。**警告を越えると、その端末に証明書の例外が残り、CA を登録しても「安全ではない」表示のままになることがあります**（Android の Firefox には例外を消す画面がありません）。登録した後に PC で `clamqtpie-ctl web tls renew` を実行し（サーバー証明書だけを作り直します。CA の登録はやり直さなくてかまいません）、ページを開き直してください

サーバー証明書は自動で作り直します。CA 証明書（期限 2 年）は自動では作り直しません。期限が近いときや、LAN のサブネットが変わったときは `web tls status` で案内するので、`clamqtpie-ctl web tls reset-ca` で作り直し、各端末から前の CA を削除して、新しい CA を登録し直してください。

#### Windows への登録

1. CA 証明書（.crt）をダブルクリックし、「証明書のインストール...」を押します
2. 保存場所は「現在のユーザー」を選びます
3. **「証明書をすべて次のストアに配置する」を選び、「参照...」で「信頼されたルート証明機関」を選びます。**「証明書の種類に基づいて、自動的に証明書ストアを選択する」を選ぶと「中間証明機関」に入り、ブラウザに信頼されません
4. 確認の画面で「はい」を押します

Firefox 系のブラウザ（Firefox・Floorp など）は、ブラウザ自身の設定（設定 →「プライバシーとセキュリティ」→「証明書を表示...」→「認証局証明書」→「インポート...」→「この認証局によるウェブサイトの識別を信頼する」）で登録します。登録した証明書は、同じ画面の「表示」で SHA-256 のフィンガープリントを確かめられます。**同じ CA が Windows の「中間証明機関」に入っていると、ブラウザで信頼しても警告が消えません**（`SEC_ERROR_UNKNOWN_ISSUER`）。`certmgr.msc` の「中間証明機関」→「証明書」に ClamQtPie の CA があれば削除してください。

#### Android への登録

機種や Android の版によって、項目の名前が少し違います。iOS では確かめていません。

1. 上の A か B で、CA 証明書のファイルを端末に保存します
2. 設定 →「セキュリティとプライバシー」→「その他のセキュリティ設定」→「暗号化と認証情報」→「証明書のインストール」→「CA 証明書」を選び、保存したファイルを選びます（設定の検索で「CA 証明書」と入れても出ます）
3. 確かめ方・削除: 「暗号化と認証情報」→「信頼できる認証情報」→「ユーザー」

**Android の Firefox** は、標準では Android に登録した CA を使いません。Firefox の設定 →「Firefox について」で Firefox のロゴを 5 回タップすると出る「Secret Settings」で、「Use third party CA certificates」を ON にし、Firefox を開き直してください。

### LAN からの接続（試験的）

> [!WARNING]
> LAN の別の端末（スマートフォンなど）から WebUI を使う機能は**試験的**です。既定では使えず、トレイGUI のメニューにも出しません。問題があれば [Issues](https://github.com/babaken/ClamQtPie/issues)（セキュリティの問題は [SECURITY.md](SECURITY.md)）で知らせてください。家庭などの信頼できるネットワークだけで使ってください。

許可する条件は、HTTPS が有効、二段階認証を設定済み、パスワードが **12 文字以上**（`web set-login` 自体は 8 文字から受け付けます。v0.10.0 より前の開発版で設定したパスワードは長さが分からないため、設定し直してください）であることです。

```sh
clamqtpie-ctl web lan on      # 許可する（Linux は sudo を付ける）
clamqtpie-ctl web status      # LAN のアドレスの URL と、注意を表示する
clamqtpie-ctl web lan off     # やめる
```

- 許可すると、WebUI が ON の間、直接つながっている LAN（プライベートの IPv4 アドレスのサブネット。VPN・仮想ネットワークは除きます）からの接続を受け付けます。WebUI を OFF にしても許可は残り、次に ON にすると LAN からも待ち受けます。トレイのメニューと WebUI の画面の上に、LAN から接続できることを表示します
- 許可している間、ログインの有効期限は 15 分です。LAN からのログインの失敗が 1 時間に 30 回になると、LAN からのログインを 15 分止めます
- HTTPS を無効にする・二段階認証を解除する・12 文字より短いパスワードにすると、許可は OFF に戻ります
- **Windows**: ファイアウォールの受信の規則 `ClamQtPie WebUI`（プライベート ネットワーク・ローカル サブネットだけ）を自動で作り、やめると消します。LAN のネットワークが「パブリック」だとつながりません（`web status` が知らせます）。信頼できるネットワークのときだけ、Windows の設定でネットワークを「プライベート」にしてください
- **Linux**: ファイアウォールは変えません。ufw・firewalld が有効なら、開け方を `web status` が表示します
- **Android**: 新しい版の Android では、ブラウザに「付近のデバイス」（ローカル ネットワーク）の権限が要ります（無いと Chrome は `ERR_TOO_MANY_RETRIES` で開けません）。`<PC の名前>.local` の名前は引けないことが多いので、`web status` が表示するアドレスの URL で開いてください

## Windows の注意事項

- トレイGUI は一般の権限で動きます（v0.9.x のような、起動のたびの UAC はありません）。UAC は、管理者の操作のときだけ出ます
- リアルタイム監視は、ファイルが保存された**後**に検査して隔離する方式です。ファイルを開く前にブロックする機能はありません
- **Microsoft Defender は止めずに併用してください。** ClamQtPie は補助として使うことを想定しています。Defender が隔離フォルダ（`%ProgramData%\ClamQtPie\quarantine`）のファイルを検知する場合は、隔離フォルダだけを Defender の除外に追加してください（ClamQtPie のフォルダ全体は除外しないでください）

## Linux の注意事項

- トレイGUI は一般の利用者で起動してください（`sudo` は不要です）
- サービスは root で動くため、clamonacc・freshclam の実行に、その都度の認証は要りません

### Linux のリアルタイム監視（clamonacc）の準備

Linux では clamonacc を使い、ファイルが開かれる**前**に検査します。

1. clamonacc（Debian / Ubuntu では `clamav-daemon` に含まれます）をインストールします
2. clamd を UNIX ソケット（`LocalSocket`）で起動します
3. `clamd.conf` に次の行を追加し、clamd を再起動します（**必須**。無いと clamonacc が起動しません）

   ```
   OnAccessExcludeUname clamav
   ```

   感染ファイルを開けないようにする場合は `OnAccessPrevention yes` も追加します

4. トレイGUI のリアルタイム保護の画面で監視するフォルダを追加し、監視を開始します

clamonacc が見つからない場合は、自動的に watchdog 方式に切り替わります。使われている方式はリアルタイム保護の画面の「方式」に表示されます。

注意点:

- `OnAccessMaxFileSize`（既定 5MB）より大きいファイルはオンアクセススキャンされません
- clamd の一時フォルダ（`TemporaryDirectory`、既定 `/tmp`）と `/` は監視するフォルダにできません（clamonacc の制限。指定しても外して監視します）
- `clamd.conf` に `OnAccessMountPath` や `OnAccessDisableDDD yes` があると、ClamQtPie で設定した監視するフォルダ・除外フォルダは使われません
- glibc 2.44 以降の環境（Arch Linux 等）では、clamonacc の不具合（[ClamAV Issue #1797](https://github.com/Cisco-Talos/clamav/issues/1797)）でサブフォルダを監視できず、除外フォルダがあると監視を開始できません。その場合はエラーが表示されます。`sudo clamqtpie-ctl config set realtime.linux_backend watchdog --restart` で watchdog 方式（ファイルの変更後に検査）にすると監視できます

## 更新・アンインストール

### 更新

新しい版のインストーラ（Windows）・`install.sh`（Linux）を、導入と同じように実行します。設定・データは残ります。サービスは止めてから入れ替え、終わったら開始します。

### アンインストール

- **Windows**: 「設定」→「アプリ」→「インストールされているアプリ」の「ClamQtPie」をアンインストールします。設定・データ・ログ（隔離したファイルを含む）も削除するかを尋ねます（既定は残す）
- **Linux**: `sudo sh /opt/clamqtpie/uninstall.sh` を実行します。データを消すかを尋ねます（`--purge` を付けると、尋ねずにデータとグループも消します）

## 困ったとき

- **トレイGUI に「ClamQtPie サービスに接続できません（E8105）」と出る**: サービスが止まっているか、まだ起動していません。`clamqtpie-ctl status`（Windows は管理者のコマンドプロンプト、Linux は `sudo clamqtpie-ctl status`）で状態を確かめてください。ログは `%ProgramData%\ClamQtPie\logs\`（Windows）、`/var/log/clamqtpie/` と `journalctl -u clamqtpie`（Linux）にあります
- **Linux で、サービスが起動の回数の上限で止まっている**: 1 日に 3 回まで起動し直しても落ちると、systemd がサービスを止めます（手での開始も断られます）。`sudo clamqtpie-ctl status` がそのことを表示します。ログで原因を確かめて直してから、`sudo systemctl reset-failed clamqtpie && sudo systemctl start clamqtpie` で開始してください
- **トレイGUI に「この PC の ClamQtPie を使う権限がありません（E8110）」と出る**: トレイGUI を使える利用者のグループに入っていないか、入れた後にサインインし直していません。トレイGUI の案内に従い、`clamqtpie-ctl users add <利用者名>` で入れるか、サインアウトしてサインインし直してください（だめなら PC を再起動）
- **WebUI に「サービスにつながりません」と出る**: WebUI が OFF になったか、サービスが止まった可能性があります。HTTPS の CA を作り直した後なら、ページを読み直してください
- **WebUI に「ログインの失敗が続いたため、しばらく時間をおいてください」と出る**: 失敗が続いてロックされています（5 分後に解けます）。確認コードの間違いが続いて止まったときは、時間がたっても解けません。上の「二段階認証」を見てください

## 開発

```bash
cd clamav_frontend
python -m venv .venv
.venv\Scripts\activate            # Linux: . .venv/bin/activate
pip install -r requirements.txt

python service_main.py --console --data-dir <確認用のフォルダ>   # サービスを前面で動かす（管理者・root で）
python tray_main.py               # トレイGUI（先にサービスを動かしておく）
python -m pytest -q               # テスト（GUI テストはヘッドレスで実行）
python build_release.py --version 0.10.0   # Windows: インストーラ、Linux: tar.gz を dist/ に作る
```

- Windows のインストーラの作成には [Inno Setup 6](https://jrsoftware.org/isinfo.php) が要ります
- リポジトリ直下の `scripts/install_service.*` で、開発中のソースのサービスを OS に登録できます（確認が終わったら `scripts/uninstall_service.*` で必ず削除してください）。`scripts/run_ClamQtPie.*` は、開発環境からトレイGUI を起動します
- テストは `tests/` 以下にあり、clamd は偽のサーバー（`FakeClamdServer`）で代用します。実際の clamd があれば、EICAR を使った結合テストも実行されます
- Windows Defender が有効な環境で EICAR のテストも実行する場合は、`clamav_frontend\.pytest_eicar` を Defender の除外に追加し、`python -m pytest --basetemp=.pytest_eicar` で実行します
- Linux で実際の clamonacc を使う結合テスト: `sudo QT_QPA_PLATFORM=offscreen <venv>/bin/python -m pytest tests/integration/test_real_clamonacc.py`

### ディレクトリ構成

```
ClamQtPie/
├── clamav_frontend/      アプリ本体
│   ├── service_main.py   サービスのエントリポイント
│   ├── tray_main.py      トレイGUI のエントリポイント
│   ├── elevate_main.py   昇格ヘルパー（管理者の操作の確認画面）
│   ├── clamqtpie_ctl.py  管理用コマンド
│   ├── api/              Web API（サービスが待ち受ける。トレイGUI・WebUI・ctl が使う）
│   ├── api_client/       トレイGUI・昇格ヘルパーの API クライアント
│   ├── core/             サービス層（clamd 通信、スキャン、リアルタイム監視、隔離、スケジューラ、通知 など）
│   ├── db/               SQLAlchemy モデルと DB サービス（起動時に Alembic で自動マイグレーション）
│   ├── gui/              トレイGUI のメインウィンドウ、10 画面、タスクトレイ
│   ├── web/              WebUI（HTML・JavaScript・CSS）
│   ├── packaging/        Windows のインストーラ（Inno Setup）、Linux の install.sh・systemd の unit
│   ├── alembic/          DB マイグレーション
│   ├── icons/            アイコン（scripts/generate_icons.py で生成）
│   ├── scripts/          アイコン生成、性能計測スクリプト
│   ├── tests/            unit / integration テスト
│   └── README_DEV.md     段階ごとの実装記録・設計判断・発見した不具合
├── scripts/              開発環境からの起動・サービスの登録スクリプト
└── docs/                 要件定義書・設計書・テスト仕様書（日本語）
```

## ドキュメント

設計書類は `docs/` にあります（日本語）。v0.10.0 のサービス化に合わせた改訂は、[インストーラ・パッケージング設計書](docs/インストーラ_パッケージング設計書.md) と [エラーコード一覧・例外設計書](docs/エラーコード一覧_例外設計書.md) から順に行っています。ほかの設計書は v0.9.x の形（1 つのアプリ）で書かれている部分があり、[開発記録（README_DEV.md）](clamav_frontend/README_DEV.md) の段階 12 の節が新しい内容です。

- [要件定義書](docs/ClamAV_Frontend_Requirements.md)
- [Linux リアルタイム監視詳細設計書（clamonacc）](docs/Linuxリアルタイム監視詳細設計書.md)
- [基本設計書](docs/基本設計書_ClamAVフロントエンド.md) / [詳細設計書](docs/詳細設計書_ClamAVフロントエンド.md) / [画面設計書](docs/画面設計書_ClamAVフロントエンド.md)
- [エラーコード一覧・例外設計書](docs/エラーコード一覧_例外設計書.md)
- [config.json スキーマ定義書](docs/config_jsonスキーマ定義書.md)
- [テスト仕様書](docs/テスト仕様書.md)
- [開発記録（README_DEV.md）](clamav_frontend/README_DEV.md)

## 不具合報告

エラーダイアログに表示されるエラーコード（例: `E1001`）と、ログ画面から出力した CSV、システム診断のレポート（トレイGUI の「レポートをコピー（パス伏せ字）」、WebUI の「伏せ字のレポートをコピー」）を添えて [Issues](https://github.com/babaken/ClamQtPie/issues) へ報告してください。

セキュリティ上の問題（脆弱性）は、公開の Issue には書かず、[SECURITY.md](SECURITY.md) の手順（GitHub の非公開の報告機能）で知らせてください。

## ライセンス

[MIT ライセンス](LICENSE)です。著作権表示を残せば、使用・改変・再配布・商用利用を自由に行えます。

配布物（インストーラ・tar.gz）には、次のライブラリが含まれます。それぞれのライセンスに従います。

| ライブラリ | ライセンス |
|---|---|
| PySide6 / Qt（shiboken6 を含む） | LGPL-3.0（配布物では DLL・共有ライブラリとして同梱しており、差し替えが可能です） |
| SQLAlchemy、Alembic、APScheduler、keyring、FastAPI、Pydantic | MIT |
| Uvicorn、Starlette、segno、psutil | BSD-3-Clause |
| watchdog | Apache-2.0 |
| cryptography | Apache-2.0 または BSD-3-Clause |
| pywin32（Windows のみ） | PSF |
| Python 本体 | PSF |

ClamAV 本体（GPL-2.0）は同梱していません。ClamQtPie は、別途インストールされた clamd とソケットで通信するだけです。

## 商標について

ClamAV は Cisco Systems, Inc. の登録商標です。本プロジェクトは ClamAV の公式プロジェクトではなく、Cisco Systems, Inc. とは関係ありません。アプリのアイコンは ClamAV の公式ロゴを使用しない独自のデザインです。
