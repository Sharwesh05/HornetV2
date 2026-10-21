import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

import app.models  # noqa: F401  (register tables)
from app.config import resolve_database_url
from app.db import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

# Migrations only need the database URL, not the full app settings (no admin key required).
URL = resolve_database_url(os.environ.get("HORNET_DATABASE_URL", "sqlite:///./hornet_dev.db"))
target_metadata = Base.metadata


def run_migrations_online() -> None:
    engine = create_engine(URL)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=URL.startswith("sqlite"))
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    context.configure(url=URL, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    run_migrations_online()
