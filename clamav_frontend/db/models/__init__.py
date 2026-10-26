"""
SQLAlchemyモデル定義

参照: DBマイグレーション設計書2.1節・7章・8章、基本設計書4章DB設計
初版（0001_initial）: quarantine / schedule / exclude
Phase5で0002_add_watch_targetにより watch_target を追加予定
"""
from __future__ import annotations

from sqlalchemy import Column, Integer, Text
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
