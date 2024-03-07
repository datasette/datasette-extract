from datasette.app import Datasette
import pytest


@pytest.mark.asyncio
async def test_extract_web():
    ds = Datasette()
    ds.add_memory_database("data")
    response = await ds.client.get(
        "/data/-/extract", cookies={"ds_actor": ds.client.actor_cookie({"id": "root"})}
    )
    assert response.status_code == 200
    assert "<h1>Extract data and create a new table in data</h1>" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actor,path,should_allow",
    (
        ("root", "/test/-/extract", True),
        ("root", "/test/foo/-/extract", True),
        ("allowed_all", "/test/-/extract", True),
        ("allowed_all", "/test/foo/-/extract", True),
        ("no_extract", "/test/-/extract", False),
        ("no_extract", "/test/foo/-/extract", False),
        ("no_insert", "/test/-/extract", True),
        ("no_insert", "/test/foo/-/extract", False),
        ("no_create", "/test/-/extract", False),
        ("no_create", "/test/foo/-/extract", True),
    ),
)
async def test_permissions(actor, path, should_allow):
    ds = Datasette(
        config={
            "permissions": {
                "insert-row": {"id": ["allowed_all", "no_create"]},
                "create-table": {"id": ["allowed_all", "no_extract", "no_insert"]},
                "datasette-extract": {
                    "id": ["allowed_all", "no_insert", "no_create", "root"]
                },
            }
        }
    )
    db = ds.add_memory_database("test")
    await db.execute_write("create table if not exists foo (id integer primary key)")
    cookies = {"ds_actor": ds.client.actor_cookie({"id": actor})}
    response = await ds.client.get(path, cookies=cookies)
    if should_allow:
        assert response.status_code == 200
    else:
        assert response.status_code == 403
