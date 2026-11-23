"""测试夹具：连本地/CI 的 Postgres，按 migrations 建表。"""
import glob
import os
import pathlib

import psycopg
import pytest

DSN = os.environ.get("PG_DSN", "")
ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def prepare_db():
    if not DSN:
        pytest.skip("未设置 PG_DSN，跳过需要数据库的测试")
    conn = psycopg.connect(DSN, autocommit=True)
    with conn, conn.cursor() as cur:
        for f in sorted(glob.glob(str(ROOT / "migrations" / "*.sql"))):
            cur.execute(pathlib.Path(f).read_text(encoding="utf-8"))
    yield
    conn.close()


@pytest.fixture()
def client():
    os.environ.setdefault("API_KEY", "test-key")
    os.environ.setdefault("OWNER", "me")
    from fastapi.testclient import TestClient

    import main
    main.API_KEY = os.environ["API_KEY"]
    main.OWNER = os.environ["OWNER"]
    main._pool = None
    with TestClient(main.app) as c:
        yield c


@pytest.fixture()
def clean_favorites():
    if not DSN:
        yield
        return
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE favorites RESTART IDENTITY")
    yield
