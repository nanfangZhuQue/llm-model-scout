import httpx
import pytest

from llm_model_scout.agent.app import create_app
from llm_model_scout.domain import EndpointConfig, ProviderConfig, ScanPolicy

TOKEN = "ui-synthetic-session-" + "t" * 32
KEY = "ui-private-provider-key"


@pytest.fixture
async def client():
    endpoint = EndpointConfig("e", "openai_chat", "https://mock.test/v1", KEY, models=("m",))
    provider = ProviderConfig("p", (endpoint,), ScanPolicy(scan_timeout=45, max_requests=5))
    transport = httpx.MockTransport(
        lambda r: httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "pong"}}]}
        )
    )
    app = create_app({"p": provider}, token=TOKEN, http_transport=transport)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8766"
        ) as test_client:
            yield test_client


@pytest.mark.parametrize(
    "path,media",
    [
        ("/", "text/html"),
        ("/ui/app.js", "javascript"),
        ("/ui/app.css", "text/css"),
        ("/favicon.svg", "image/svg+xml"),
    ],
)
async def test_public_static_whitelist(client, path, media):
    response = await client.get(path)
    assert response.status_code == 200
    assert media in response.headers["content-type"]
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["referrer-policy"] == "no-referrer"
    assert TOKEN not in response.text and KEY not in response.text
    assert (await client.get(path, headers={"Origin": "https://evil.test"})).status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/providers",
        "/scans",
        "/ui/missing",
        "/.scout/agent.token",
        "/ui/../.claude/settings.local.json",
    ],
)
async def test_static_exemption_does_not_open_api_or_files(client, path):
    assert (await client.get(path)).status_code in (401, 404)
    if path not in ("/providers", "/scans"):
        assert (
            await client.get(path, headers={"Authorization": f"Bearer {TOKEN}"})
        ).status_code == 404


async def test_inventory_limits_and_task_list(client):
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    inventory = (await client.get("/providers")).json()["providers"][0]
    assert inventory["limits"]["scan_timeout"] == 45
    assert inventory["limits"]["max_models"] == 1000
    assert inventory["endpoints"][0]["probe"]["max_tokens"] == 1
    assert inventory["endpoints"][0]["has_manual_models"] is True
    ids = [
        (await client.post("/scans", json={"provider_id": "p", "models": ["m"]})).json()["scan_id"]
        for _ in range(3)
    ]
    response = await client.get("/scans?limit=2&offset=0")
    data = response.json()
    assert [j["scan_id"] for j in data["scans"]] == ids[::-1][:2]
    assert data["total"] == 3
    assert len((await client.get("/scans?limit=2&offset=2")).json()["scans"]) == 1
    assert KEY not in response.text and TOKEN not in response.text
    assert "base_url" not in str(inventory) and "api_key" not in str(inventory)
    assert (await client.get("/scans?limit=101")).status_code == 422
    assert (await client.get("/scans?offset=-1")).status_code == 422
