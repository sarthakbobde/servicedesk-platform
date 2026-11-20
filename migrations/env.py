import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from ticketing import models  # noqa: F401  (registers tables)
from ticketing.config import get_config
from ticketing.db import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def url() -> str:
    return config.attributes.get("url") or os.environ.get("TICKETING_DATABASE_URL") or get_config().database_url


def run_migrations_offline():
    context.configure(url=url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = config.attributes.get("connection")
    if connectable is not None:
        context.configure(connection=connectable, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
