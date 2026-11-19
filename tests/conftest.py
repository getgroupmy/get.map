import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "integrations" / "python"))
FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture
def waze_routes():
    return load("get_route_routes_london.json")


@pytest.fixture
def db_url():
    """A Postgres with migration 001 applied, from TEST_DATABASE_URL; the map
    schema is emptied before each test. Skipped when not set."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    import psycopg
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("drop schema if exists map cascade")
        c.execute((ROOT / "db/migrations/001_map_capture.sql").read_text())
    return url
