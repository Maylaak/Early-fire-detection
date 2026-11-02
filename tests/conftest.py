import os

import pytest

from gateway.db import init_schema, make_engine, metadata


@pytest.fixture(params=["sqlite", "postgres"])
def engine(request, tmp_path):
    """A fresh database: always SQLite; also Postgres when TEST_DATABASE_URL is set.
    WARNING: the Postgres variant drops and recreates the tables in that database."""
    if request.param == "sqlite":
        eng = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    else:
        url = os.environ.get("TEST_DATABASE_URL")
        if not url:
            pytest.skip("TEST_DATABASE_URL not set")
        eng = make_engine(url)
        metadata.drop_all(eng)
    init_schema(eng)
    yield eng
    eng.dispose()
