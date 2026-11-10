import os
import uuid

import pytest
from fastapi.testclient import TestClient

from quality_api.auth import issue_token
from quality_api.cli import migrate
from quality_api.config import Settings
from quality_api.db import Database
from quality_api.main import create_app
from quality_api.models import Base


@pytest.fixture
def settings(tmp_path):
    return Settings(environment="test", allow_sqlite=True,
                    database_url=os.environ.get("QUALITY_TEST_DATABASE_URL", f"sqlite:///{tmp_path / 'quality.db'}"),
                    artifact_dir=tmp_path / "artifacts", human_jwt_secret="human-test-" + "h" * 40,
                    model_jwt_secret="model-test-" + "m" * 40, trace_jwt_secret="trace-test-" + "t" * 40,
                    worker_jwt_secret="worker-test-" + "w" * 40, recovery_epoch="test-epoch",
                    tool_registry_json='[{"id":"fixture_read","version":"1","effect":"read","shareable":false,"input_schema":{"type":"object"}}]')


@pytest.fixture
def database(settings):
    db = Database(settings.database_url)
    # Real Postgres runs use a dedicated test database only, never production env.
    if db.engine.dialect.name == 'postgresql' and not (db.engine.url.database or '').endswith('_test'):
        pytest.fail('Destructive integration tests require a dedicated database name ending in _test')
    Base.metadata.drop_all(db.engine)
    with db.engine.begin() as connection:
        from sqlalchemy import text
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    migrate(db)
    yield db
    db.engine.dispose()


@pytest.fixture
def db_factory(database):
    return database.Session


@pytest.fixture
def client(settings, database):
    app = create_app(settings, database)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def headers(settings):
    def make(kind="human", user_id="alice", chat_id="chat-1", case_id=None, run_id=None,
             key=None, instance_id="test-instance", **extra):
        token = issue_token(settings, kind=kind, user_id=user_id, instance_id=instance_id,
                            chat_id=chat_id, case_id=case_id, run_id=run_id, **extra)
        return {"Authorization": "Bearer " + token, "Idempotency-Key": key or str(uuid.uuid4())}
    return make
