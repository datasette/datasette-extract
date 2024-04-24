import asyncio
from datasette.app import Datasette
import pytest


@pytest.mark.vcr(ignore_localhost=True)
@pytest.mark.asyncio
async def test_extract_flow():
    ds = Datasette()
    ds.add_memory_database("data")
    cookies = {"ds_actor": ds.client.actor_cookie({"id": "root"})}
    response = await ds.client.get("/data/-/extract", cookies=cookies)
    assert response.status_code == 200
    assert "<h1>Extract data and create a new table in data</h1>" in response.text
    csrftoken = response.cookies["ds_csrftoken"]
    cookies["ds_csrftoken"] = csrftoken
    # Now submit a POST, then wait 30s
    post_response = await ds.client.post(
        "/data/-/extract",
        data={
            "table": "ages",
            "content": "Sergei is 4, Cynthia is 7",
            "csrftoken": csrftoken,
            "name_0": "name",
            "type_0": "string",
            "name_1": "age",
            "type_1": "integer",
            "instructions": "Be nice",
        },
        files={
            # Send an empty image too
            "image": b""
        },
        cookies=cookies,
    )
    assert post_response.status_code == 302
    redirect_url = post_response.headers["location"]
    assert redirect_url.startswith("/-/extract/progress/")
    task_id = redirect_url.split("/")[-1]
    poll_url = redirect_url + ".json"
    # Wait a moment for ds._extract_tasks to be populated
    await asyncio.sleep(0.5)
    assert task_id in ds._extract_tasks
    # Now we poll for completion
    data = None
    while True:
        poll_response = await ds.client.get(poll_url)
        data = poll_response.json()
        if data["done"]:
            break
        await asyncio.sleep(1)

    assert data == {
        "items": [{"name": "Sergei", "age": 4}, {"name": "Cynthia", "age": 7}],
        "instructions": "Be nice",
        "database": "data",
        "table": "ages",
        "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
        "error": None,
        "done": True,
    }


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

    # Also check if the action items were visible
    if path == "/test/-/extract":
        fetch_path = "/test"
    else:
        fetch_path = "/test/foo"
    html = (await ds.client.get(fetch_path, cookies=cookies)).text
    fragment = f'<a href="{path}"'
    if should_allow:
        assert fragment in html
    else:
        assert fragment not in html


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ("/test2", "/test2/foo"))
@pytest.mark.parametrize("has_env_variable", (True, False))
async def test_action_menus_require_api_key(monkeypatch, path, has_env_variable):
    if not has_env_variable:
        monkeypatch.delenv("DATASETTE_SECRETS_OPENAI_API_KEY")
    ds = Datasette(
        config={
            "permissions": {
                "datasette-extract": {"id": "root"},
            }
        }
    )
    db = ds.add_memory_database("test2")
    await db.execute_write("create table if not exists foo (id integer primary key)")
    cookies = {"ds_actor": ds.client.actor_cookie({"id": "root"})}
    response = await ds.client.get(path, cookies=cookies)

    fragment = '/-/extract"'
    if has_env_variable:
        assert fragment in response.text
    else:
        assert fragment not in response.text
