"""
ConfigService

参照: config.jsonスキーマ定義書2章・3章・7章
- サービスの設定（schema_version 2）。利用者ごとの GUI の設定（general）は gui.json に分けたため、読み込んでも
  使わず、保存もしない
- アトミック書き込み（tmp→rename）
- SMTP パスワードは config.json に保存せず、秘密の保存先（core/secret_store.py の FileSecretStore）に置く

段階 12-4e で従来のアプリ（ポータブル、1 プロセス）を削除し、schema_version 1 の保存と keyring の扱いを除いた
（旧版の keyring のパスワードは clamqtpie-ctl migrate が読む。core/portable_migration.py）。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from core.config_model import GUI_SECTIONS, SCHEMA_VERSION, ConfigModel, validate_and_fix
from core.exceptions import ConfigError, ConfigWriteError, KeyringUnavailableError

#: サービスの秘密の保存先での、SMTP パスワードの名前
_SMTP_SECRET_NAME = "smtp_password"
_TOTP_SECRET_NAME = "web_totp_secret"

logger = logging.getLogger("clamav_frontend")


class ConfigService:
    """サービスの config.json の読み書きと、SMTP パスワードの保存を担う"""

    def __init__(self, config_path: Path, secret_store=None) -> None:
        """secret_store: SMTP パスワードの保存先（core/secret_store.py の FileSecretStore）。None なら保存先が無く、
        パスワードは「なし」として扱い、保存しようとすると KeyringUnavailableError（テスト・設定だけを読む用途）"""
        self.config_path = config_path
        self._model: ConfigModel | None = None
        self._secret_store = secret_store

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

        if model.schema_version > SCHEMA_VERSION:
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
        model.schema_version = SCHEMA_VERSION
        self._model = model
        payload = json.dumps(self.as_dict(model), ensure_ascii=False, indent=2)

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

    def as_dict(self, model: ConfigModel | None = None) -> dict:
        """保存する内容（利用者ごとの GUI の設定を除く）"""
        data = (model or self.current).to_dict()
        for section in GUI_SECTIONS:
            data.pop(section, None)
        return data

    @property
    def current(self) -> ConfigModel:
        if self._model is None:
            return self.load()
        return self._model

    # -- SMTP パスワード ---------------------------------------------
    def get_smtp_password(self) -> str | None:
        if self._secret_store is None:
            return None
        return self._secret_store.get(_SMTP_SECRET_NAME)

    def set_smtp_password(self, password: str) -> None:
        if self._secret_store is None:
            raise KeyringUnavailableError(detail="SMTP パスワードの保存先がありません")
        self._secret_store.set(_SMTP_SECRET_NAME, password)

    def delete_smtp_password(self) -> None:
        if self._secret_store is not None:
            self._secret_store.delete(_SMTP_SECRET_NAME)

    # -- 二段階認証の秘密鍵（段階 12-5d。設定ファイルには入れない） -----------------
    def get_totp_secret(self) -> str | None:
        if self._secret_store is None:
            return None
        return self._secret_store.get(_TOTP_SECRET_NAME)

    def set_totp_secret(self, secret: str) -> None:
        if self._secret_store is None:
            raise KeyringUnavailableError(detail="二段階認証の秘密鍵の保存先がありません")
        self._secret_store.set(_TOTP_SECRET_NAME, secret)

    def delete_totp_secret(self) -> None:
        if self._secret_store is not None:
            self._secret_store.delete(_TOTP_SECRET_NAME)
