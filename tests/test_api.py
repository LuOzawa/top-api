"""API 契约测试。需要数据库的用 PG_DSN 提供，CI 里跑真实 Postgres。"""
import os

import pytest

KEY = "test-key"
H = {"X-API-Key": KEY}


def test_health_no_db(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_requires_api_key(client):
    r = client.post("/api/favorites", json={"url": "https://example.com/x"})
    assert r.status_code == 401


def test_invalid_key(client):
    r = client.post("/api/favorites", json={"url": "https://example.com/x"},
                    headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


def test_url_validation(client):
    for bad in ["not-a-url", "ftp://example.com", "javascript:alert(1)", ""]:
        r = client.post("/api/favorites", json={"url": bad}, headers=H)
        assert r.status_code == 422, bad


def test_note_too_long(client):
    r = client.post("/api/favorites",
                    json={"url": "https://example.com/a", "note": "x" * 1001}, headers=H)
    assert r.status_code == 422


@pytest.mark.usefixtures("clean_favorites")
def test_crud_flow(client):
    if not os.environ.get("PG_DSN"):
        pytest.skip("需要数据库")

    # 创建
    r = client.post("/api/favorites",
                    json={"url": "https://example.com/post/1", "title": "T1", "platform": "hn"},
                    headers=H)
    assert r.status_code == 201
    body = r.json()
    fav_id = body["id"]
    assert body["created"] is True

    # 幂等：重复提交不新增
    r2 = client.post("/api/favorites",
                     json={"url": "https://example.com/post/1", "platform": "hn"}, headers=H)
    assert r2.status_code == 200
    assert r2.json()["created"] is False
    assert r2.json()["id"] == fav_id
    assert client.get("/api/favorites", headers=H).json()["total"] == 1

    # 列表 + 分页字段
    lst = client.get("/api/favorites", headers=H).json()
    assert lst["items"][0]["url"] == "https://example.com/post/1"

    # 更新
    r3 = client.patch(f"/api/favorites/{fav_id}", json={"note": "记一下"}, headers=H)
    assert r3.status_code == 200
    assert r3.json()["note"] == "记一下"

    # 空更新应 400
    assert client.patch(f"/api/favorites/{fav_id}", json={}, headers=H).status_code == 400

    # 统计
    st = client.get("/api/favorites/stats", headers=H).json()
    assert st["total"] == 1 and st["by_platform"][0]["platform"] == "hn"

    # 平台过滤
    assert client.get("/api/favorites?platform=nope", headers=H).json()["total"] == 0

    # 删除
    assert client.delete(f"/api/favorites/{fav_id}", headers=H).status_code == 204
    assert client.delete(f"/api/favorites/{fav_id}", headers=H).status_code == 404
    assert client.get("/api/favorites", headers=H).json()["total"] == 0
