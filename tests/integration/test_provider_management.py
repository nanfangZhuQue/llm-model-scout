import asyncio
from contextlib import asynccontextmanager
from io import BytesIO

import httpx
import pytest
from cryptography.fernet import Fernet
from openpyxl import load_workbook

from llm_model_scout.agent.app import create_app
from llm_model_scout.agent.history import HistoryStore
from llm_model_scout.providers import ProviderStore

TOKEN = "managed-provider-token-" + "x" * 32
KEY = "synthetic-managed-key-123456"


@pytest.fixture
async def managed(tmp_path):
    protector = Fernet(Fernet.generate_key())
    store = ProviderStore(tmp_path / "providers.db", protector)
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "model-a"}, {"id": "model-b"}]})
        if request.url.path.endswith("/responses"):
            return httpx.Response(
                200,
                json={
                    "object": "response",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [{"type": "output_text", "text": "pong"}],
                        }
                    ],
                },
            )
        if request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "pong"}],
                },
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "pong"}}]}
        )

    app = create_app(
        {},
        token=TOKEN,
        provider_store=store,
        http_transport=httpx.MockTransport(handler),
        history_store=HistoryStore(tmp_path / "scans.sqlite3"),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://127.0.0.1:8766",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client:
            yield client, store, calls


def payload():
    return {
        "name": "我的服务商",
        "endpoints": [
            {"protocol": "openai_chat", "base_url": "https://upstream.test/v1", "api_key": KEY},
            {
                "protocol": "anthropic_messages",
                "base_url": "https://upstream.test/v1",
                "api_key": KEY,
            },
        ],
    }


async def wait_done(client, scan_id):
    async with asyncio.timeout(3):
        while True:
            job = (await client.get(f"/scans/{scan_id}")).json()
            if job["state"] in ("completed", "failed", "cancelled"):
                return job
            await asyncio.sleep(0.005)


async def test_provider_save_discover_select_export_and_delete(managed):
    client, store, calls = managed
    response = await client.post("/providers", json=payload())
    assert response.status_code == 201
    assert KEY not in response.text
    provider_id = response.json()["provider_id"]
    assert store.records()[provider_id]["definition"]["endpoints"][0]["api_key"] == KEY
    assert KEY.encode() not in store.path.read_bytes()
    discovery = await client.post(
        "/scans", json={"provider_id": provider_id, "endpoint_id": "all", "operation": "models"}
    )
    assert discovery.status_code == 202
    discovery_id = discovery.json()["scan_id"]
    assert (await wait_done(client, discovery_id))["operation"] == "models"
    report = (await client.get(f"/scans/{discovery_id}/report")).json()
    assert len(report["discovery"]) == 2 and not report["results"]
    assert len(calls) == 2 and all(r.method == "GET" for r in calls)
    test = await client.post(
        "/scans",
        json={
            "provider_id": provider_id,
            "endpoint_id": "all",
            "model_selections": {"openai": ["model-a"]},
        },
    )
    test_id = test.json()["scan_id"]
    await wait_done(client, test_id)
    result = (await client.get(f"/scans/{test_id}/report")).json()
    assert len(result["results"]) == 1 and result["results"][0]["status"] == "available"
    public = await client.post(f"/providers/{provider_id}/export", json={})
    assert KEY not in public.text and public.json()["contains_secrets"] is False
    denied = await client.post(f"/providers/{provider_id}/export", json={"include_secrets": True})
    assert denied.status_code == 422 and KEY not in denied.text
    private = await client.post(
        f"/providers/{provider_id}/export",
        json={"include_secrets": True, "confirm_plaintext": True},
    )
    assert private.json()["provider"]["endpoints"][0]["api_key"] == KEY
    assert ".private.json" in private.headers["content-disposition"]
    edited = payload()
    edited["revision"] = 1
    edited["name"] = "修改后的名称"
    for endpoint in edited["endpoints"]:
        endpoint.pop("api_key")
    assert (await client.put(f"/providers/{provider_id}", json=edited)).status_code == 200
    assert (await client.put(f"/providers/{provider_id}", json=edited)).status_code == 409
    assert (await client.delete(f"/providers/{provider_id}?revision=2")).status_code == 200
    assert not store.records()
    assert (await client.get("/providers")).json()["providers"] == []
    assert (await client.get(f"/scans/{test_id}")).status_code == 200
    assert KEY not in (await client.get("/scans")).text
    saved = HistoryStore(store.path.with_name("scans.sqlite3")).get(test_id)
    assert saved["view"]["provider_id"] == provider_id
    assert saved["view"]["provider_name"] == "我的服务商"
    assert saved["conditions"]["provider_revision"] == 1
    (endpoint,) = saved["conditions"]["endpoints"]
    assert endpoint["id"] == "openai" and endpoint["models"] == ["model-a"]


async def test_three_managed_protocols_preserve_selection_auth_and_report(managed):
    client, store, calls = managed
    definition = payload()
    responses_key = "synthetic-responses-key-123456"
    definition["endpoints"].append(
        {
            "protocol": "openai_responses",
            "base_url": "https://upstream.test/v1",
            "api_key": responses_key,
        }
    )
    response = await client.post("/providers", json=definition)
    assert response.status_code == 201
    provider = response.json()
    provider_id = provider["provider_id"]
    assert [(item["id"], item["protocol"]) for item in provider["endpoints"]] == [
        ("openai", "openai_chat"),
        ("anthropic", "anthropic_messages"),
        ("responses", "openai_responses"),
    ]
    for row in definition["endpoints"]:
        row.pop("api_key")
    edited = {**definition, "revision": provider["revision"], "name": "Edited"}
    response = await client.put(f"/providers/{provider_id}", json=edited)
    assert response.status_code == 200 and responses_key not in response.text
    assert responses_key.encode() not in store.path.read_bytes()

    selection = {
        "provider_id": provider_id,
        "endpoint_id": "all",
        "max_requests": 3,
        "model_selections": dict.fromkeys(("openai", "anthropic", "responses"), ["model-a"]),
    }
    preview = await client.post("/scans/preview", json=selection)
    assert preview.status_code == 200 and preview.json()["target_count"] == 3
    assert preview.json()["request_upper_bound"] == 3 and not calls
    scan = await client.post("/scans", json=selection)
    assert scan.status_code == 202
    scan_id = scan.json()["scan_id"]
    assert (await wait_done(client, scan_id))["state"] == "completed"
    report = (await client.get(f"/scans/{scan_id}/report")).json()
    assert report["summary"]["available"] == 3 and report["scan"]["requests_sent"] == 3
    assert {(row["endpoint_id"], row["model"]) for row in report["results"]} == {
        ("openai", "model-a"),
        ("anthropic", "model-a"),
        ("responses", "model-a"),
    }
    by_path = {request.url.path: request for request in calls}
    assert by_path["/v1/chat/completions"].headers["authorization"] == f"Bearer {KEY}"
    assert by_path["/v1/messages"].headers["x-api-key"] == KEY
    assert by_path["/v1/responses"].headers["authorization"] == f"Bearer {responses_key}"
    assert "x-api-key" not in by_path["/v1/responses"].headers

    xlsx = await client.get(f"/scans/{scan_id}/report?format=xlsx")
    assert xlsx.status_code == 200
    workbook = load_workbook(BytesIO(xlsx.content), read_only=True)
    header, *rows = workbook.active.values
    protocol = header.index("协议")
    assert {row[protocol] for row in rows} == {
        "Anthropic Messages",
        "OpenAI Chat",
        "OpenAI Responses",
    }
    workbook.close()


async def test_history_after_provider_rename_and_delete(tmp_path):
    protector = Fernet(Fernet.generate_key())
    store = ProviderStore(tmp_path / "providers.db", protector)
    history_path = tmp_path / "scans.sqlite3"
    calls = []

    def verify(request):
        calls.append(request)
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "pong"}}]}
        )

    @asynccontextmanager
    async def session():
        app = create_app(
            {},
            token=TOKEN,
            provider_store=store,
            history_store=HistoryStore(history_path),
            http_transport=httpx.MockTransport(verify),
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url="http://127.0.0.1:8766",
                headers={"Authorization": f"Bearer {TOKEN}"},
            ) as client:
                yield client

    async with session() as client:
        definition = payload()
        created = await client.post("/providers", json=definition)
        assert created.status_code == 201
        provider_id = created.json()["provider_id"]
        selection = {"provider_id": provider_id, "endpoint_id": "openai", "models": ["m"]}
        first = await client.post("/scans", json=selection)
        first_id = first.json()["scan_id"]
        assert (await wait_done(client, first_id))["state"] == "completed"
        definition.update(name="修改后的名称", revision=1)
        assert (await client.put(f"/providers/{provider_id}", json=definition)).status_code == 200
        second = await client.post("/scans", json=selection)
        second_id = second.json()["scan_id"]
        assert (await wait_done(client, second_id))["state"] == "completed"
        assert (await client.delete(f"/providers/{provider_id}?revision=2")).status_code == 200

    async with session() as client:
        assert (await client.get("/providers")).json()["providers"] == []
        page = (await client.get("/scans", params={"provider_id": provider_id, "limit": 2})).json()
        assert page["total"] == 2
        assert page["providers"] == [{"provider_id": provider_id, "provider_name": "修改后的名称"}]
        assert [(row["scan_id"], row["provider_name"]) for row in page["scans"]] == [
            (second_id, "修改后的名称"),
            (first_id, "我的服务商"),
        ]
        detail = await client.get(f"/scans/{first_id}")
        assert detail.json()["provider_name"] == "我的服务商"
        report = await client.get(f"/scans/{first_id}/report")
        assert report.status_code == 200 and report.json()["provider"]["name"] == "我的服务商"
        assert KEY not in report.text and len(calls) == 2
        comparison = await client.get(
            "/scans/compare",
            params={"baseline_id": first_id, "current_id": second_id},
        )
        assert comparison.status_code == 200
        compared = comparison.json()
        assert compared["endpoint_id"] == "openai"
        assert compared["conditions"]["status"] == "changed"
        assert compared["conditions"]["credentials"] == "unknown"
        assert compared["conditions"]["changes"] == [
            {"field": "provider_revision", "baseline": 1, "current": 2}
        ]
        assert compared["rows"][0]["verification"] == "conditions_changed"
        assert KEY not in comparison.text and len(calls) == 2


async def test_management_auth_and_validation(managed):
    client, store, _ = managed
    assert (
        await client.post("/providers", json=payload(), headers={"Authorization": ""})
    ).status_code == 401
    assert (
        await client.post("/providers", json=payload(), headers={"Origin": "https://evil.test"})
    ).status_code == 403
    bad = payload()
    bad["endpoints"][0]["api_key"] = ""
    response = await client.post("/providers", json=bad)
    assert response.status_code == 422 and KEY not in response.text
    assert not store.records()
