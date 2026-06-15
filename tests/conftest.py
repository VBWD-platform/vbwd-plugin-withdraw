"""Shared fixtures for withdraw tests.

Provides the session-scoped `app` + `client` fixtures route/integration
specs request by name. Service-level unit specs use fakes/`MagicMock`
collaborators and never load this app.
"""
import os
import sys

import pytest


# Ensure the project root is on sys.path so plugin modules import as
# `plugins.withdraw.…` regardless of where pytest was invoked from.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

os.environ.setdefault("FLASK_ENV", "testing")
os.environ.setdefault("TESTING", "true")


def _test_db_url() -> str:
    """Use a sibling `<dbname>_test` database so the integration tests don't
    collide with whatever the api container is doing in the main `vbwd` DB."""
    base = os.getenv("DATABASE_URL", "postgresql://vbwd:vbwd@postgres:5432/vbwd")
    prefix, _, dbname = base.rpartition("/")
    dbname = dbname.split("?")[0]
    return f"{prefix}/{dbname}_test"


def _ensure_test_db(url: str) -> None:
    from sqlalchemy import create_engine, text

    main_url = url.rsplit("/", 1)[0] + "/postgres"
    dbname = url.rsplit("/", 1)[1].split("?")[0]
    engine = create_engine(main_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": dbname}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def app():
    """Full Flask app on a `<dbname>_test` database with the full schema built
    once for the session via the shared integration-db helper — self-bootstrapping,
    no dependency on the api container's `alembic upgrade heads`."""
    from vbwd.app import create_app
    from vbwd.extensions import db as _db

    test_url = _test_db_url()
    _ensure_test_db(test_url)
    application = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": test_url,
            "SQLALCHEMY_TRACK_MODIFICATIONS": False,
            "WTF_CSRF_ENABLED": False,
            "RATELIMIT_ENABLED": False,
            "RATELIMIT_STORAGE_URL": "memory://",
        }
    )
    with application.app_context():
        # Importing the model package registers withdraw_request with
        # SQLAlchemy so create_all builds it too.
        import importlib

        importlib.import_module("plugins.withdraw.withdraw.models")

        # Build the schema once per process (create_all, checkfirst — never
        # drops, so it cannot wipe data) and commit baseline reference rows
        # once. Each test then isolates itself via a rolled-back transaction
        # (no TRUNCATE, no DROP) — see vbwd/testing/integration_db.py.
        from vbwd.testing.integration_db import ensure_schema_and_baseline

        ensure_schema_and_baseline(_db)

    yield application

    with application.app_context():
        _db.engine.dispose()


@pytest.fixture(autouse=True)
def _isolate_test(app, request):
    """Isolate every test in a rolled-back transaction (self-cleaning, no wipe).

    Autouse so route specs that request only ``client`` are isolated too; nothing
    a test writes persists past it (the rollback IS the cleanup). The schema +
    baseline reference rows are built once in the ``app`` fixture. See
    vbwd/testing/integration_db.py.

    A test marked ``no_db_isolation`` (e.g. the migration spec that opens its own
    connection and rolls back itself) runs WITHOUT the wrapper, keeping
    ``db.engine`` a real Engine.
    """
    from vbwd.extensions import db as _db

    if request.node.get_closest_marker("no_db_isolation") is not None:
        with app.app_context():
            yield
            _db.session.remove()
        return

    with app.app_context():
        from vbwd.testing.integration_db import rollback_isolation

        with rollback_isolation(_db):
            yield


@pytest.fixture
def client(app):
    return app.test_client()
