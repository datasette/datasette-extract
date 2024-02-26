import pytest


@pytest.fixture(autouse=True)
def mock_api_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "mock-api-key")
