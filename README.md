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

## 概要

ClamQtPie は、オープンソースのウイルス対策エンジン [ClamAV](https://www.clamav.net/) を、タスクトレイに常駐するデスクトップアプリとして使えるようにする GUI フロントエンドです。Python と PySide6（Qt）で作られており、Windows と Linux の両方で動作します。

ClamAV エンジン自体は同梱しません。別途インストールした clamd と通信して動作します。

## 主な機能

| 機能 | 内容 |
|---|---|
| オンデマンドスキャン | フォルダ・ファイルを指定して検査。進捗と現在のファイルをリアルタイム表示 |
| リアルタイム監視 | Windows: watchdog でファイルの作成・変更を検知して検査<br>Linux: ClamAV 公式の **clamonacc**（オンアクセススキャン）と連携 |
| 隔離 | 検知ファイルを隔離フォルダへ移動。一覧・復元・完全削除 |
| スケジュール | cron 形式でスキャン・定義更新を定期実行（同じジョブの重複実行は防止） |
| 定義更新 | freshclam による手動更新・自動更新と、更新履歴の表示 |
| 通知 | デスクトップ通知（Windows: Toast / Linux: notify-send）とメール通知（SMTP）。Windows では検知の通知をクリックすると隔離画面が開く |
| 除外設定 | ファイル・フォルダ・拡張子・プロセス単位で除外 |
| タスクトレイ常駐 | 状態別アイコン（通常 / 監視中 / 検知あり / エラー）、多重起動防止、OS 起動時の自動起動（設定画面で保存するとすぐ反映） |
| ログ | 検知・スキャン・隔離・更新などをカテゴリ付きで記録。フィルタ・CSV 出力。エラーコード（例: `E1001`）から該当ログへ移動 |
| clamd の起動 | clamd が停止していればサービスとして起動を試みる（「ClamAV エンジン サービス」画面の「サービスを開始」ボタン、停止を検知したときに1回自動。この画面から停止した場合は自動では起動し直さない） |

## 動作要件

- **OS**: Windows 10 / 11（64bit）、または Linux
- **ClamAV**: 別途インストールが必要です（下記「必要な ClamAV コンポーネント」）
- **開発時**: Python 3.14 以降（3.15 でも動作確認済み）

### 必要な ClamAV コンポーネント

| コンポーネント | Windows | Linux | 用途 |
|---|---|---|---|
| clamd（検査エンジン） | **必須**。OS のサービス `clamd` として登録 | **必須**。`clamav-daemon` サービス | すべての検査（スキャン・リアルタイム監視）を clamd に依頼します |
| freshclam（定義更新） | **必須**。アプリが直接実行します（定義更新のサービスは不要） | **必須**。自動更新・スケジュールの定義更新を使う場合は `clamav-freshclam` サービスも必須 | ウイルス定義の更新 |
| clamonacc | 使いません | リアルタイム監視を clamonacc 方式で使う場合 | 下記「Linux のリアルタイム監視（clamonacc）の準備」 |
| clamscan / clamdscan | 使いません | 使いません | 障害を調べるときにだけ使います |

- clamd が停止していると検査はできません（画面・トレイ・定義更新・隔離の管理・ログは使えます）。停止を検知すると、アプリが OS のサービスとして起動を1回試みます
- アプリは freshclam と clamonacc を **PATH** から探します。PATH に無い場合は、設定画面でフルパスを指定してください（freshclam は「定義更新」タブ、clamonacc は「ClamAV エンジン」タブ）
- clamd の接続先の既定値は、Windows が TCP `127.0.0.1:3310`、Linux が UNIX ソケット `/var/run/clamav/clamd.ctl` です。`clamd.conf` で別の値（`TCPSocket` / `TCPAddr` / `LocalSocket`）にした場合は、設定画面の「ClamAV エンジン」タブ（`config.json` の `clamd.tcp_host` / `clamd.tcp_port` / `clamd.unix_socket_path`）も同じ値にしてください。「接続テスト」で保存前に接続を試せます。接続の設定はアプリの再起動後に反映されます。Linux のソケットの場所はディストリビューションによって異なるため、`clamd.conf` の `LocalSocket` を確認してください
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

5. インストール先を環境変数 PATH に追加します（または、設定画面の「定義更新」タブで freshclam の場所を指定します）

### Linux の準備

`apt install clamav clamav-daemon`（Debian / Ubuntu 系）、`pacman -S clamav`（Arch 系）などでインストールし、clamd と定義更新のサービスを有効にします（下記「Linux の注意事項」）。

配布版を X11 のデスクトップで使う場合は、Qt が必要とする `libxcb-cursor0`（Arch 系は `xcb-util-cursor`）も入れてください。Wayland のデスクトップでは不要です。

### 準備の確認

アプリの「システム診断」画面で「ヘルスチェックを実行」を押し、[NG] や [注意] が無いことを確認します。[注意] には、PATH に無い実行ファイルや、`clamd.conf` の待ち受け先とアプリの接続先の食い違いが表示されます。

### 動作確認済みの環境

| 環境 | 内容 |
|---|---|
| Windows 11 Pro | Python 3.14.8 / 3.15.0、PySide6 6.12.0、ClamAV 1.5.4 |
| Linux（WSL2 Ubuntu 26.04） | Python 3.14、PySide6 6.12.0、ClamAV 1.5.4（clamonacc 含む） |

## 使い方（配布版）

インストーラはありません（ポータブルな Zip 配布です）。

1. リリースの Zip を展開します
   - **Windows: `C:\Program Files\ClamQtPie\` のような、管理者だけが書き込める場所**に展開してください（展開には管理者権限が必要です）。アプリは管理者権限で動くため、一般ユーザーが書き換えられる場所に置くと、そのフォルダの中身や設定を書き換えられたときに、管理者権限で悪用されるおそれがあります
   - Windows で避ける場所: デスクトップ・ダウンロード・ドキュメント、USB メモリ、`C:\` の直下に自分で作ったフォルダ（`C:\Tools` など。既定の設定では一般ユーザーも書き込めます）
   - Linux: ホームフォルダの下など、自分だけが書き込める場所に展開してください（他の利用者も書き込める共有フォルダは避けてください）
   - どちらも、OneDrive・Dropbox・Google ドライブなどの**同期フォルダには置かないでください**。隔離したファイル（`quarantine/`）が同期先や他の PC に広がります。バックアップの対象からも `quarantine/` を外してください
2. `ClamQtPie.exe`（Windows）または `ClamQtPie`（Linux）を起動します

### ポータブル構成

- 初回起動時に `config.json`・`app.db`・`logs/`・`quarantine/` が実行ファイルと同じフォルダに作られます
- フォルダごとコピーすれば、別の PC へ設定を持ち運べます（コピー先も上の「展開する場所」の条件を満たす場所にしてください）
- **SMTP パスワードだけは、コピー先で再設定が必要です**（OS のキーチェーン（Windows: 資格情報マネージャー、Linux: Secret Service）に保存されるため）

### Windows の注意事項

- アプリは**常に管理者権限で起動**するため、起動のたびに UAC の確認画面が出ます。隔離（ファイル権限の変更）、リアルタイム監視、自動起動の設定（レジストリへの書き込み）に管理者権限が必要なためです
- 設定画面で「OS起動時自動起動」を有効にした場合も、**OS の起動のたびに UAC の確認画面が出ます**（Windows の仕様です）。「いいえ」を選ぶと、そのセッションでは保護機能が動きません
- リアルタイム監視は、ファイルが保存された**後**に検査して隔離する方式です。ファイルを開く前にブロックする機能はありません
- **Microsoft Defender は止めずに併用してください。** ClamQtPie は補助として使うことを想定しています。Defender が隔離フォルダ（`quarantine/`）のファイルを検知する場合は、隔離フォルダだけを Defender の除外に追加してください（ClamQtPie のフォルダ全体は除外しないでください）

### Linux の注意事項

- アプリは**一般ユーザーで起動**してください（`sudo` は不要です）。root 権限が必要な処理（clamd の起動、clamonacc、定義更新）は、その都度 pkexec の認証画面が出ます
- clamd と定義更新は OS のサービスに任せることを推奨します

  ```sh
  sudo systemctl enable --now clamav-daemon.service clamav-freshclam.service
  ```

  サービス名はディストリビューションによって異なる場合があります（Arch では `clamav-daemon.service` / `clamav-freshclam.service`）
- 定義更新の動作
  - 定義更新画面・ホーム画面・トレイからの「今すぐ更新」は、管理者認証（pkexec）をしてから freshclam を実行します
  - 自動更新・スケジュールの定義更新は、無人で認証できないため freshclam を実行しません。`clamav-freshclam` サービスが動いていればスキップし（サービスが更新します）、動いていなければサービスの有効化を案内して失敗とします

### バージョンアップ

1. 新しいバージョンの Zip を別のフォルダへ展開します
2. 設定を引き継ぐ場合は、古いフォルダの `config.json` を新しいフォルダへコピーします（SMTP パスワードは再設定が必要です）
3. 古いフォルダを削除します

旧名（ClamAVフロントエンド、`ClamAVFrontend.exe`）の版から移る場合も、手順は同じです。新しい版を初めて起動すると、旧名で登録した OS 起動時の自動起動・SMTP パスワード・通知のクリックで隔離画面を開くための設定を、自動で引き継ぎます。

### アンインストール

1. タスクトレイのアイコンを右クリックし、「終了」でアプリを終了します
2. 自動起動を有効にしていた場合は、設定画面の「一般」タブで「OS起動時自動起動」をオフにして保存します
3. 展開したフォルダごと削除します
4. （Windows）通知のクリックで隔離画面を開くための設定を削除します。PowerShell で次を実行します

   ```powershell
   Remove-Item -Recurse "HKCU:\Software\Classes\clamqtpie", "$env:LOCALAPPDATA\clamqtpie"
   ```

### Linux のリアルタイム監視（clamonacc）の準備

Linux では clamonacc を使い、ファイルが開かれる**前**に検査します。

1. clamonacc（Debian / Ubuntu では `clamav-daemon` に含まれます）と pkexec（polkit）をインストールします
2. clamd を UNIX ソケット（`LocalSocket`）で起動します
3. `clamd.conf` に次の行を追加し、clamd を再起動します（**必須**。無いと clamonacc が起動しません）

   ```
   OnAccessExcludeUname clamav
   ```

   感染ファイルを開けないようにする場合は `OnAccessPrevention yes` も追加します

4. アプリのリアルタイム画面で監視を開始すると、管理者認証を求められます（clamonacc の実行に root 権限が必要なため）

clamonacc または pkexec が見つからない場合は、自動的に watchdog 方式に切り替わります。使われている方式はリアルタイム画面の「方式」に表示されます。

注意点:

- `OnAccessMaxFileSize`（既定 5MB）より大きいファイルはオンアクセススキャンされません
- clamd の一時フォルダ（`TemporaryDirectory`、既定 `/tmp`）と `/` は監視対象にできません（clamonacc の制限。指定しても外して監視します）
- `clamd.conf` に `OnAccessMountPath` や `OnAccessDisableDDD yes` があると、アプリで設定した監視対象・除外フォルダは使われません
- glibc 2.44 以降の環境（Arch Linux 等）では、clamonacc の不具合（[ClamAV Issue #1797](https://github.com/Cisco-Talos/clamav/issues/1797)）でサブフォルダを監視できず、除外フォルダがあると監視を開始できません。その場合はエラーが表示されます。`config.json` の `realtime.linux_backend` を `"watchdog"` にすると watchdog 方式（ファイルの変更後に検査）で監視できます

## 開発

```bash
cd clamav_frontend
python -m venv .venv
.venv\Scripts\activate            # Linux: . .venv/bin/activate
pip install -r requirements.txt

python app.py                     # 起動
python -m pytest -q               # テスト（GUI テストはヘッドレスで実行）
python build_release.py --version 0.9.1   # PyInstaller でビルドし、dist/ に Zip を作成
```

- リポジトリ直下の `scripts/run_ClamQtPie.bat`（Windows）・`scripts/run_ClamQtPie.sh`（Linux）で、どこからでも `clamav_frontend/.venv` の Python でアプリを起動できます（Linux で venv が別の場所にあるときは `CLAMQTPIE_PYTHON` に Python のパスを指定）。Windows で管理者として動かすときは、`.bat` を右クリックして「管理者として実行」を選びます
- テストは `tests/` 以下にあり、clamd は偽のサーバー（`FakeClamdServer`）で代用します。実際の clamd があれば、EICAR を使った結合テストも実行されます
- Windows Defender が有効な環境で EICAR のテストも実行する場合は、`clamav_frontend\.pytest_eicar` を Defender の除外に追加し、`python -m pytest --basetemp=.pytest_eicar` で実行します
- Linux で実際の clamonacc を使う結合テスト: `sudo QT_QPA_PLATFORM=offscreen <venv>/bin/python -m pytest tests/integration/test_real_clamonacc.py`

### ディレクトリ構成

```
ClamQtPie/
├── clamav_frontend/      アプリ本体
│   ├── app.py            エントリポイント（サービスの生成と画面への結線）
│   ├── core/             サービス層（clamd 通信、スキャン、リアルタイム監視、隔離、スケジューラ、通知 など）
│   ├── db/               SQLAlchemy モデルと DB サービス（起動時に Alembic で自動マイグレーション）
│   ├── gui/              メインウィンドウ、8 画面、タスクトレイ
│   ├── alembic/          DB マイグレーション
│   ├── icons/            アイコン（scripts/generate_icons.py で生成）
│   ├── scripts/          アイコン生成、性能計測スクリプト
│   ├── tests/            unit / integration テスト
│   └── README_DEV.md     フェーズごとの実装記録・設計判断・発見した不具合
├── scripts/              開発環境からの起動スクリプト
└── docs/                 要件定義書・設計書・テスト仕様書（日本語）
```

## ドキュメント

設計書類は `docs/` にあります（日本語）。主なもの:

- [要件定義書](docs/ClamAV_Frontend_Requirements.md)
- [Linux リアルタイム監視詳細設計書（clamonacc）](docs/Linuxリアルタイム監視詳細設計書.md)
- [基本設計書](docs/基本設計書_ClamAVフロントエンド.md) / [詳細設計書](docs/詳細設計書_ClamAVフロントエンド.md) / [画面設計書](docs/画面設計書_ClamAVフロントエンド.md)
- [エラーコード一覧・例外設計書](docs/エラーコード一覧_例外設計書.md)
- [config.json スキーマ定義書](docs/config_jsonスキーマ定義書.md)
- [テスト仕様書](docs/テスト仕様書.md)
- [開発記録（README_DEV.md）](clamav_frontend/README_DEV.md)

## 不具合報告

エラーダイアログに表示されるエラーコード（例: `E1001`）と、ログ画面から出力した CSV を添えて [Issues](https://github.com/babaken/ClamQtPie/issues) へ報告してください。

セキュリティ上の問題（脆弱性）は、公開の Issue には書かず、[SECURITY.md](SECURITY.md) の手順（GitHub の非公開の報告機能）で知らせてください。

## ライセンス

[MIT ライセンス](LICENSE)です。著作権表示を残せば、使用・改変・再配布・商用利用を自由に行えます。

配布版（Zip）には、次のライブラリが含まれます。それぞれのライセンスに従います。

| ライブラリ | ライセンス |
|---|---|
| PySide6 / Qt（shiboken6 を含む） | LGPL-3.0（配布版では DLL として同梱しており、差し替えが可能です） |
| SQLAlchemy、Alembic、APScheduler、keyring | MIT |
| watchdog | Apache-2.0 |
| pywin32（Windows のみ） | PSF |
| Python 本体 | PSF |

ClamAV 本体（GPL-2.0）は同梱していません。ClamQtPie は、別途インストールされた clamd とソケットで通信するだけです。

## 商標について

ClamAV は Cisco Systems, Inc. の登録商標です。本プロジェクトは ClamAV の公式プロジェクトではなく、Cisco Systems, Inc. とは関係ありません。アプリのアイコンは ClamAV の公式ロゴを使用しない独自のデザインです。
