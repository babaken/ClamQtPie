"""
Alembic実行環境設定

参照: DBマイグレーション設計書2.2節
- 常にオンラインモードで実行する（オフラインモード/SQLスクリプト手動適用は行わない）
- DatabaseServiceから直接呼び出す場合は config.attributes["connection"] に
  既存のSQLAlchemy Connectionを渡すことで、アプリのDB接続をそのまま使い回す
"""
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

from db.models import Base

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False: アプリ本体（core.logger）が設定した
    # "clamav_frontend" ロガーがAlembic実行によって無効化されないようにする
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """本アプリではオフラインモードは使用しない方針（DBマイグレーション設計書2.2節）が、
    Alembic CLIから直接叩かれた場合のフォールバックとして最低限実装しておく。
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # SQLite向け（DBマイグレーション設計書2.2節）
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection", None)

    if connectable is None:
        # Alembic CLIから単独実行された場合（開発時のスクリプト作成等）
        connectable = engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    else:
        # DatabaseServiceから既存Connectionを渡された場合（アプリ組み込み実行）
        context.configure(
            connection=connectable,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
