from datasette.app import Datasette
import pytest


@pytest.mark.asyncio
async def test_extract_web():
    ds = Datasette(memory=True)
    response = await ds.client.get("/-/extract")
    assert response.status_code == 200
    assert "<h1>Extract</h1>" in response.text
