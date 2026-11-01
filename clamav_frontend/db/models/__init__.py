"""
SQLAlchemyモデル定義

参照: DBマイグレーション設計書2.1節・7章・8章、基本設計書4章DB設計
初版（0001_initial）: quarantine / schedule / exclude
Phase5で0002_add_watch_targetにより watch_target を追加予定
"""
from __future__ import annotations

from sqlalchemy import Column, ForeignKey, Integer, LargeBinary, Text
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Quarantine(Base):
    __tablename__ = "quarantine"

    id = Column(Integer, primary_key=True)
    uuid = Column(Text)
    original_path = Column(Text)
    virus_name = Column(Text)
    sha256 = Column(Text)
    size = Column(Integer)
    detected_at = Column(Text)
    restored = Column(Integer, default=0)
    deleted = Column(Integer, default=0)
    # 段階 12-2d（0005）: 隔離ファイルの変換の方式（"none" | "xor-v1"）、元の所有者・権限（JSON）、
    # UTF-8 にできない名前の元のバイト列（その場合 original_path は見える形にした表示用）
    encoding = Column(Text, nullable=False, default="none", server_default="none")
    owner_info = Column(Text)
    original_path_raw = Column(LargeBinary)


class Schedule(Base):
    __tablename__ = "schedule"

    id = Column(Integer, primary_key=True)
    name = Column(Text)
    enabled = Column(Integer, default=1)
    cron = Column(Text)
    target = Column(Text)
    scan_type = Column(Text)
    last_run = Column(Text)
    next_run = Column(Text)


class Exclude(Base):
    __tablename__ = "exclude"

    id = Column(Integer, primary_key=True)
    type = Column(Text)  # "file" | "folder" | "extension" | "process"
    value = Column(Text)
    enabled = Column(Integer, default=1)


class WatchTarget(Base):
    """リアルタイム監視対象パス

    参照: Windowsリアルタイム監視詳細設計書3.2節（DBマイグレーション設計書8章で追加）
    """

    __tablename__ = "watch_target"

    id = Column(Integer, primary_key=True)
    path = Column(Text)
    enabled = Column(Integer, default=1)
    added_at = Column(Text)


class UpdateHistory(Base):
    """定義DB更新履歴

    参照: 画面設計書6章「更新履歴」、画面設計書9章 未確定事項1
    （専用DBテーブルとして持つ方針をPhase6で確定）
    """

    __tablename__ = "update_history"

    id = Column(Integer, primary_key=True)
    executed_at = Column(Text)
    result = Column(Text)  # "success" | "failure" | "no_change"
    before_version = Column(Text)
    after_version = Column(Text)


class ScanHistory(Base):
    """スキャン履歴（サービス化・Web API設計書 6.2節。0004_add_scan_history）

    手動（画面・Web API）とスケジュールのすべてのスキャンを、終了時に1行記録する。
    """

    __tablename__ = "scan_history"

    id = Column(Integer, primary_key=True)
    started_at = Column(Text, index=True)
    finished_at = Column(Text)
    source = Column(Text)  # "gui" | "api" | "schedule"
    scan_type = Column(Text)  # "quick" | "full" | "custom" | "schedule"
    label = Column(Text)
    targets = Column(Text)  # JSON の配列
    scanned = Column(Integer, default=0)
    detected = Column(Integer, default=0)
    errors = Column(Integer, default=0)
    errors_truncated = Column(Integer, default=0)  # 記録の上限を超えて、ファイルを残さなかったエラーの数
    result = Column(Text)  # "completed" | "cancelled" | "failed"
    detail = Column(Text)  # 失敗の理由


class ScanHistoryDetection(Base):
    __tablename__ = "scan_history_detection"

    id = Column(Integer, primary_key=True)
    history_id = Column(Integer, ForeignKey("scan_history.id", ondelete="CASCADE"), nullable=False, index=True)
    path = Column(Text)
    virus_name = Column(Text)
    action = Column(Text)  # "quarantined" | "notified"


class ScanHistoryError(Base):
    """検査できなかったファイル（12章 46。サイズの上限超えなどで検査を逃れる手口があるため、理由とともに残す）"""

    __tablename__ = "scan_history_error"

    id = Column(Integer, primary_key=True)
    history_id = Column(Integer, ForeignKey("scan_history.id", ondelete="CASCADE"), nullable=False, index=True)
    path = Column(Text)
    reason = Column(Text)  # "size_limit" | "permission" | "vanished" | "other"
    detail = Column(Text)
