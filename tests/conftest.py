import pytest


@pytest.fixture(autouse=True)
def mock_api_key(monkeypatch):
    monkeypatch.setenv("DATASETTE_SECRETS_OPENAI_API_KEY", "mock-api-key")
    monkeypatch.setenv("OPENAI_API_KEY", "mock-api-key")
