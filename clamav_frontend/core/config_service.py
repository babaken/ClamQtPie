"""
ConfigService

参照: config.jsonスキーマ定義書2章・3章・7章
- ポータブル配置（実行ファイルと同一ディレクトリ）
- アトミック書き込み（tmp→rename）
- SMTPパスワードはkeyring経由（config.jsonには保存しない）
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from core.config_model import ConfigModel, validate_and_fix
from core.exceptions import ConfigError, ConfigWriteError, KeyringUnavailableError
from core.version import APP_ID, LEGACY_APP_ID

_KEYRING_SERVICE_NAME = APP_ID
#: 製品名の変更前のサービス名（migrate_legacy_smtp_passwordで新しいサービス名へ移す）
_LEGACY_KEYRING_SERVICE_NAME = LEGACY_APP_ID

logger = logging.getLogger("clamav_frontend")


class ConfigService:
    """config.jsonの読み書きとSMTPパスワードのkeyring連携を担う"""

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self._model: ConfigModel | None = None

    # -- 読み込み -----------------------------------------------------
    def load(self) -> ConfigModel:
        """config.jsonを読み込む。存在しない場合は既定値で新規作成する。

        参照: config.jsonスキーマ定義書2.3節
        """
        if not self.config_path.exists():
            model = ConfigModel()
            self._model = model
            self.save(model)
            return model

        try:
            raw_text = self.config_path.read_text(encoding="utf-8")
            raw = json.loads(raw_text)
        except (OSError, json.JSONDecodeError) as exc:
            # E6004: config.json破損。既定値で再生成し、破損ファイルは退避する
            backup_path = self.config_path.with_suffix(".json.bak")
            try:
                self.config_path.replace(backup_path)
            except OSError:
                pass
            logger.warning(
                "設定ファイルを読み込めませんでした。初期設定で起動します "
                f"(detail: {exc}, backup: {backup_path})",
                extra={"error_code": "E6004"},
            )
            model = ConfigModel()
            self._model = model
            self.save(model)
            return model

        model = ConfigModel.from_dict(raw)

        if model.schema_version > ConfigModel().schema_version:
            logger.warning(
                "新しいバージョンで作成された設定ファイルの可能性があります "
                f"(schema_version={model.schema_version})。上書き保存は行いません。"
            )
            self._model = model
            return model

        model = validate_and_fix(model, warn=lambda msg: logger.warning(msg))

        # 欠損キー補完後の内容を書き戻す（config.jsonスキーマ定義書2.3節）
        self._model = model
        self.save(model)
        return model

    # -- 保存 -----------------------------------------------------
    def save(self, model: ConfigModel) -> None:
        """アトミック書き込み（一時ファイル→リネーム）でconfig.jsonへ保存する

        参照: config.jsonスキーマ定義書2.3節
        """
        self._model = model
        payload = json.dumps(model.to_dict(), ensure_ascii=False, indent=2)

        target_dir = self.config_path.parent
        target_dir.mkdir(parents=True, exist_ok=True)

        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=target_dir, prefix=".config_", suffix=".json.tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                os.replace(tmp_name, self.config_path)
            except Exception:
                if os.path.exists(tmp_name):
                    os.remove(tmp_name)
                raise
        except OSError as exc:
            raise ConfigWriteError(detail=str(exc)) from exc

    @property
    def current(self) -> ConfigModel:
        if self._model is None:
            return self.load()
        return self._model

    # -- SMTPパスワード（keyring連携） ---------------------------------
    def get_smtp_password(self) -> str | None:
        """keyring経由でSMTPパスワードを取得する（config.jsonスキーマ定義書3章）"""
        try:
            import keyring

            account = self.current.smtp.keyring_account
            return keyring.get_password(_KEYRING_SERVICE_NAME, account)
        except Exception as exc:  # keyringバックエンド不在等
            raise KeyringUnavailableError(detail=str(exc)) from exc

    def set_smtp_password(self, password: str) -> None:
        try:
            import keyring

            account = self.current.smtp.keyring_account
            keyring.set_password(_KEYRING_SERVICE_NAME, account, password)
        except Exception as exc:
            raise KeyringUnavailableError(detail=str(exc)) from exc

    def delete_smtp_password(self) -> None:
        try:
            import keyring

            account = self.current.smtp.keyring_account
            keyring.delete_password(_KEYRING_SERVICE_NAME, account)
        except Exception as exc:
            # 未設定状態での削除等は致命的ではないためWARNINGに留める
            logger.warning(f"SMTPパスワードの削除に失敗しました (detail: {exc})")

    def migrate_legacy_smtp_password(self) -> bool:
        """旧名のサービス名で保存したSMTPパスワードを、新しいサービス名へ移す。移したらTrue

        新しいサービス名に既にパスワードがある場合は上書きしない（旧名の分だけ消す）。
        keyringが使えない環境では何もしない（起動は止めない）。
        """
        try:
            import keyring

            account = self.current.smtp.keyring_account
            legacy = keyring.get_password(_LEGACY_KEYRING_SERVICE_NAME, account)
            if legacy is None:
                return False
            if keyring.get_password(_KEYRING_SERVICE_NAME, account) is None:
                keyring.set_password(_KEYRING_SERVICE_NAME, account, legacy)
            keyring.delete_password(_LEGACY_KEYRING_SERVICE_NAME, account)
            return True
        except Exception as exc:  # noqa: BLE001 - keyringバックエンド不在等
            logger.warning(f"旧名で保存したSMTPパスワードを引き継げませんでした (detail: {exc})")
            return False
