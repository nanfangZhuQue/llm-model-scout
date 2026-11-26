import os

import pytest

from llm_model_scout.core.redact import Redactor
from llm_model_scout.domain import EndpointConfig, ProviderConfig
from llm_model_scout.providers import ProviderStore, StoreConflict, export_definition


@pytest.fixture
def definition():
    return {
        "name": "测试服务",
        "endpoints": [
            {
                "protocol": "openai_chat",
                "base_url": "https://example.test/v1",
                "api_key": "synthetic-provider-secret-123",
                "proxy": "http://user:proxy-secret@127.0.0.1:1080",
            }
        ],
    }


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI integration")
def test_dpapi_persistence_revision_and_explicit_export(tmp_path, definition):
    path = tmp_path / "providers.sqlite3"
    store = ProviderStore(path)
    key = store.put(definition)
    raw = path.read_bytes()
    assert b"synthetic-provider-secret-123" not in raw
    assert b"proxy-secret" not in raw and b"example.test" not in raw
    loaded = ProviderStore(path).records()[key]
    assert loaded["definition"]["endpoints"][0]["api_key"] == "synthetic-provider-secret-123"
    assert loaded["revision"] == 1
    public = export_definition(loaded["definition"])
    assert "synthetic-provider-secret-123" not in str(public)
    assert "proxy-secret" not in str(public)
    private = export_definition(loaded["definition"], True)
    assert private["contains_secrets"] is True
    assert private["provider"]["endpoints"][0]["api_key"] == "synthetic-provider-secret-123"
    with pytest.raises(StoreConflict):
        store.put(definition, key, revision=99)
    store.put({**definition, "name": "Renamed"}, key, revision=1)
    assert ProviderStore(path).records()[key]["revision"] == 2
    with pytest.raises(StoreConflict):
        store.delete(key, 1)
    store.delete(key, 2)
    assert not store.records()


def test_short_proxy_credentials_do_not_corrupt_protocols():
    endpoint = EndpointConfig(
        "openai",
        "openai_chat",
        "https://example.test/v1",
        "synthetic-api-key",
        proxy="http://u:p@127.0.0.1:8080",
    )
    redactor = Redactor.for_provider(ProviderConfig("provider", (endpoint,)))
    assert redactor.text("openai_chat skipped 2026-10-05") == "openai_chat skipped 2026-10-05"
    assert "u:p" not in redactor.text("http://u:p@127.0.0.1:8080")
