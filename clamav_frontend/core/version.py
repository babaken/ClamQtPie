"""
製品名・バージョン・OSに登録する識別子

タイトルバー・ヘッダー・ステータスバー・トレイ・通知・診断レポートで共通に使う。
製品名はClamQtPie。ClamAV（Cisco社の商標）の公式製品と誤認されないよう、
ClamAVの名前は「ClamAV フロントエンド」「Powered by ClamAV」のように説明として添えるだけにする。

製品名の変更（ClamAVフロントエンド → ClamQt → ClamQtPie）に伴い、OSに登録する識別子も揃えた。
旧名（LEGACY_*）で登録された内容は、起動時にcore/legacy_migration.pyで新しい名前へ引き継ぐ。
"""
from __future__ import annotations

APP_NAME = "ClamQtPie"
APP_SUBTITLE = "ClamAV フロントエンド"
APP_POWERED_BY = "Powered by ClamAV"
#: 配布物の版。build_release.pyの--versionの既定値で、異なる値を指定するとビルドを中止する
APP_VERSION = "1.0.0"

#: OSに登録する識別子（キーリングのサービス名・自動起動のレジストリ値・トースト通知の差出人・ミューテックス）
APP_ID = "ClamQtPie"
#: ユーザー単位のフォルダ名（%LOCALAPPDATA%・~/.local/share）・URLスキーム・自動起動の.desktopの名前
APP_SLUG = "clamqtpie"

#: 旧名で登録された識別子（起動時に新しい名前へ引き継ぐ。core/legacy_migration.py）
LEGACY_APP_ID = "ClamAVFrontend"
LEGACY_APP_SLUG = "clamav-frontend"
LEGACY_PROTOCOL = "clamqt"
