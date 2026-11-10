from alembic import context

from quality_api.models import Base

config = context.config
connection = config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Run migrations via python -m quality_api.cli migrate")
context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
with context.begin_transaction():
    context.run_migrations()
