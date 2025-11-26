import pytest


@pytest.fixture(autouse=True)
def mock_api_key(monkeypatch):
    monkeypatch.setenv("DATASETTE_SECRETS_OPENAI_API_KEY", "mock-api-key")
    monkeypatch.setenv("OPENAI_API_KEY", "mock-api-key")


@pytest.fixture(scope="module")
def vcr_config():
    return {"filter_headers": ["authorization"]}


@pytest.fixture(autouse=True)
def mock_pricing_cache():
    """Pre-populate pricing cache so no HTTP request is made during tests."""
    import datasette_llm_accountant.pricing as pricing_module

    pricing_module._pricing_cache = {
        "gpt-4.1-mini": {
            "id": "gpt-4.1-mini",
            "vendor": "openai",
            "name": "GPT-4.1 Mini",
            "input": 0.4,
            "output": 1.6,
            "input_cached": 0.1,
        },
    }
