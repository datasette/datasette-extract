import pytest


@pytest.fixture(autouse=True)
def mock_api_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "mock-api-key")


@pytest.fixture(scope="module")
def vcr_config():
    return {"filter_headers": ["authorization"]}
