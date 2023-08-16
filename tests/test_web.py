from datasette.app import Datasette
import pytest


@pytest.mark.asyncio
async def test_extract_web():
    ds = Datasette(memory=True)
    ds.add_memory_database("data")
    response = await ds.client.get("/-/extract/data")
    assert response.status_code == 200
    assert "<h1>Extract data and create a new table in data</h1>" in response.text
