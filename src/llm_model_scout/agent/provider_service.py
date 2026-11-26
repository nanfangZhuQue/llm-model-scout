import asyncio

from ..core.redact import Redactor
from ..providers import StoreError, definition_to_provider, export_definition
from ..transport import RequestGate
from .manager import TERMINAL, TaskError


class ProviderService:
    def __init__(self, manager, store):
        self.manager = manager
        self.store = store
        self.static_ids = set(manager.registry)
        self.managed_ids = set()
        self.records = {}
        self.lock = asyncio.Lock()
        if store is not None:
            self.apply(store.records())

    def active(self, provider_id):
        return any(
            j.provider_id == provider_id and j.state not in TERMINAL
            for j in self.manager.jobs.values()
        )

    def apply(self, records):
        if set(records) & self.static_ids:
            raise StoreError("Saved provider identifiers conflict with configured providers")
        retained = set(records)
        for key in self.managed_ids - set(records):
            if self.active(key):
                retained.add(key)
            else:
                self.manager.registry.pop(key, None)
                self.manager.gates.pop(key, None)
        for key, record in records.items():
            provider = definition_to_provider(record["definition"])
            self.manager.registry[key] = provider
            if key not in self.manager.gates:
                self.manager.gates[key] = RequestGate(provider.policy)
        self.managed_ids = retained
        self.records = records

    async def refresh(self):
        if self.store is not None:
            async with self.lock:
                self.apply(await asyncio.to_thread(self.store.records))

    def visible(self, key):
        return key in self.static_ids or key in self.records

    def inventory(self):
        values = []
        for item in self.manager.inventory():
            key = item["provider_id"]
            if not self.visible(key):
                continue
            item["name"] = self.manager.registry[key].name
            item["editable"] = key in self.records
            if key in self.records:
                record = self.records[key]
                public = export_definition(record["definition"])["provider"]
                for endpoint in public["endpoints"]:
                    endpoint.pop("api_key", None)
                    endpoint["has_key"] = True
                item.update(name=public["name"], revision=record["revision"], connection=public)
            values.append(Redactor.for_provider(self.manager.registry[key]).clean(item))
        return values

    async def save(self, definition, provider_id=None, revision=None):
        if self.store is None:
            raise TaskError(403, "Provider management is disabled in configuration-only mode")
        async with self.lock:
            records = await asyncio.to_thread(self.store.records)
            if provider_id is not None:
                if provider_id not in records:
                    raise TaskError(404, "Unknown saved provider")
                if self.active(provider_id):
                    raise TaskError(409, "Wait for this provider's active tasks before editing")
                old = {e["protocol"]: e for e in records[provider_id]["definition"]["endpoints"]}
                for endpoint in definition["endpoints"]:
                    if not endpoint.get("api_key"):
                        endpoint["api_key"] = old.get(endpoint["protocol"], {}).get("api_key")
                    if "proxy" not in endpoint:
                        endpoint["proxy"] = old.get(endpoint["protocol"], {}).get("proxy")
            key = await asyncio.to_thread(self.store.put, definition, provider_id, revision)
            self.apply(await asyncio.to_thread(self.store.records))
            return next(item for item in self.inventory() if item["provider_id"] == key)

    async def delete(self, provider_id, revision):
        if self.store is None or provider_id not in self.records:
            raise TaskError(404, "Unknown saved provider")
        async with self.lock:
            if self.active(provider_id):
                raise TaskError(409, "Wait for active tasks before deleting this provider")
            await asyncio.to_thread(self.store.delete, provider_id, revision)
            self.apply(await asyncio.to_thread(self.store.records))

    async def export(self, provider_id, include_secrets):
        await self.refresh()
        record = self.records.get(provider_id)
        if record is None:
            raise TaskError(404, "Unknown saved provider")
        return export_definition(record["definition"], include_secrets)
