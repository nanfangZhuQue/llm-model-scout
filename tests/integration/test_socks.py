import asyncio
import json
import socket
import ssl
from contextlib import asynccontextmanager

import pytest
import trustme

from llm_model_scout.core.scanner import scan
from llm_model_scout.domain import EndpointConfig, ProviderConfig, ScanPolicy
from llm_model_scout.report import report_json


@asynccontextmanager
async def socks_proxy(target_port, *, require_auth=False, deny=False):
    connections = []
    tasks = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        upstream = None
        try:
            version, nmethods = await reader.readexactly(2)
            assert version == 5
            methods = await reader.readexactly(nmethods)
            method = 2 if require_auth else 0
            assert method in methods
            writer.write(bytes([5, method]))
            await writer.drain()
            if require_auth:
                version, nuser = await reader.readexactly(2)
                user = await reader.readexactly(nuser)
                npass = (await reader.readexactly(1))[0]
                password = await reader.readexactly(npass)
                valid = user == b"proxy-user" and password == b"proxy-pass"
                writer.write(bytes([1, 0 if valid else 1]))
                await writer.drain()
                if not valid:
                    return
            version, command, _, atyp = await reader.readexactly(4)
            assert version == 5 and command == 1
            if atyp == 3:
                size = (await reader.readexactly(1))[0]
                host = (await reader.readexactly(size)).decode()
            elif atyp == 1:
                host = socket.inet_ntoa(await reader.readexactly(4))
            else:
                host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
            port = int.from_bytes(await reader.readexactly(2), "big")
            connections.append((atyp, host, port))
            if deny:
                writer.write(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()
                return
            remote_reader, upstream = await asyncio.open_connection("127.0.0.1", target_port)
            writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            await writer.drain()

            async def pipe(source, dest):
                while data := await source.read(65536):
                    dest.write(data)
                    await dest.drain()

            relays = [
                asyncio.create_task(pipe(reader, upstream)),
                asyncio.create_task(pipe(remote_reader, writer)),
            ]
            try:
                await asyncio.wait(relays, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for relay in relays:
                    relay.cancel()
                await asyncio.gather(*relays, return_exceptions=True)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            if upstream:
                upstream.close()
                await upstream.wait_closed()
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1], connections
    finally:
        server.close()
        await server.wait_closed()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@asynccontextmanager
async def tls_target(tmp_path):
    ca = trustme.CA()
    certificate = ca.issue_cert("target.invalid")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    certificate.configure_cert(ctx)
    ca_path = tmp_path / "ca.pem"
    ca.cert_pem.write_to_path(ca_path)
    calls = []

    async def handle(reader, writer):
        try:
            raw_headers = await reader.readuntil(b"\r\n\r\n")
            lines = raw_headers.decode().split("\r\n")
            method, path, _ = lines[0].split()
            headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            length = int(next((v for k, v in headers.items() if k.lower() == "content-length"), 0))
            body = await reader.readexactly(length)
            calls.append((method, path, headers, body))
            if method == "GET":
                doc = {"data": [{"id": "good"}]}
            elif path.endswith("/messages"):
                doc = {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "pong"}],
                }
            else:
                doc = {"choices": [{"message": {"role": "assistant", "content": "pong"}}]}
            payload = json.dumps(doc).encode()
            writer.write(
                (
                    f"HTTP/1.1 200 OK\r\nContent-Length: {len(payload)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode()
                + payload
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
    try:
        yield server.sockets[0].getsockname()[1], str(ca_path), calls
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages"])
@pytest.mark.parametrize("authenticated", [False, True])
async def test_https_discovery_and_verification_over_socks(tmp_path, protocol, authenticated):
    async with tls_target(tmp_path) as (target_port, ca_path, calls):
        async with socks_proxy(target_port, require_auth=authenticated) as (port, connections):
            auth = "proxy-user:proxy-pass@" if authenticated else ""
            endpoint = EndpointConfig(
                "test",
                protocol,
                f"https://target.invalid:{target_port}/v1",
                "synthetic-api-key",
                proxy=f"socks5://{auth}127.0.0.1:{port}",
                ca_bundle=ca_path,
            )
            report = await scan(ProviderConfig("local", (endpoint,), ScanPolicy(retries=0)))
            assert report.results[0].status == "available"
            assert len(calls) == 2
            # .invalid cannot resolve locally: SOCKS receives the hostname, not a local DNS answer.
            assert all(atyp == 3 and host == "target.invalid" for atyp, host, _ in connections)
            assert "proxy-pass" not in report_json(report)


async def test_bad_proxy_auth_does_not_fallback(http_server):
    base, calls = http_server
    async with socks_proxy(1, require_auth=True) as (port, _):
        endpoint = EndpointConfig(
            "test",
            "openai_chat",
            base + "/v1",
            "synthetic-api-key",
            proxy=f"socks5://proxy-user:wrong-pass@127.0.0.1:{port}",
            models=("good",),
        )
        report = await scan(ProviderConfig("local", (endpoint,), ScanPolicy(retries=0)))
        assert report.results[0].error_category == "NETWORK_ERROR"
        assert not calls
        assert "wrong-pass" not in report_json(report)


async def test_proxy_refused_target_does_not_fallback(http_server):
    base, calls = http_server
    async with socks_proxy(1, deny=True) as (port, _):
        endpoint = EndpointConfig(
            "test",
            "openai_chat",
            base + "/v1",
            "synthetic-api-key",
            proxy=f"socks5://127.0.0.1:{port}",
            models=("good",),
        )
        report = await scan(ProviderConfig("local", (endpoint,), ScanPolicy(retries=0)))
        assert report.results[0].error_category == "NETWORK_ERROR"
        assert not calls
