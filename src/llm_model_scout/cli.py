from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import __version__
from .config import ConfigError, load_provider
from .core.scanner import scan
from .report import report_json, write_report

app = typer.Typer(
    help="Verify models through explicit OpenAI or Anthropic endpoints.",
    pretty_exceptions_enable=False,
)

console = Console()


class ConsoleScanProgress:
    """Display the scanner's plan and the verification results seen so far."""

    def __init__(self, *, stderr: bool = False):
        self.stderr = stderr
        self.discovered = 0
        self.planned = None
        self.finished = 0

    def emit(self, line: str):
        typer.echo(line, err=self.stderr)

    def verification(self) -> str:
        if self.planned is None:
            return "验证计划尚未形成"
        if not self.planned:
            return "没有符合条件的验证目标"
        percent = self.finished * 100 / self.planned
        return f"已验证 {self.finished}/{self.planned}（{percent:.1f}%）"

    def event(self, event):
        if event["event"] == "discovered":
            self.discovered += event["count"]
            self.emit(
                f"发现阶段 · {event['endpoint_id']} 有 {event['count']} 个候选"
                f"（累计 {self.discovered}）"
            )
        elif event["event"] == "planned":
            self.planned = event["count"]
            self.emit(f"验证阶段 · {self.verification()}")
        elif event["event"] == "result":
            if event["status"] in ("available", "unavailable"):
                self.finished += 1
            self.emit(
                f"{self.verification()} · {event['endpoint_id']}:"
                f" {event['model']} {event['status']}"
            )

    def finish(self, report, *, discover_only: bool = False):
        state = {"completed": "完成", "failed": "未完成", "cancelled": "已取消"}[report.status]
        reason = f"（{report.reason}）" if report.reason else ""
        if discover_only or self.planned is None:
            self.emit(f"发现{state} · 已发现 {self.discovered} 个候选{reason}")
            return
        results = [r for r in report.results if r.reason != "model_budget_exhausted"]
        self.finished = sum(r.status in ("available", "unavailable") for r in results)
        skipped = sum(r.status == "skipped" for r in results)
        cancelled = sum(r.status == "cancelled" for r in results)
        limit = len(report.results) - len(results)
        limited = f" · 超出模型上限 {limit} 项" if limit else ""
        self.emit(
            f"扫描{state} · {self.verification()} · 跳过 {skipped} 项"
            f" · 取消 {cancelled} 项{limited}{reason}"
        )


def _status_style(status: str) -> str:
    return {
        "available": "bold green",
        "unavailable": "bold red",
        "skipped": "yellow",
        "cancelled": "dim",
    }.get(status, "white")


def _error_style(category: str | None) -> str:
    if category in ("OK", None):
        return "green"
    if category in ("UNAUTHORIZED", "FORBIDDEN", "PAYMENT_REQUIRED"):
        return "bold red"
    if category in ("RATE_LIMITED", "SERVER_ERROR", "TIMEOUT", "NETWORK_ERROR"):
        return "orange3"
    return "red"


def print_pretty_report(report):
    """Print a colored table summary of the scan report."""
    header = Text()
    header.append("Model Scout ", style="bold cyan")
    header.append(f"v{__version__}", style="cyan")
    header.append(f"  Provider: {report.provider}", style="white")
    header.append(f"  Status: {report.status}", style=_status_style(report.status))
    console.print(header)
    console.print()

    total = len(report.results)
    avail = sum(1 for r in report.results if r.status == "available")
    unavail = sum(1 for r in report.results if r.status == "unavailable")
    skipped = sum(1 for r in report.results if r.status == "skipped")
    cancelled = sum(1 for r in report.results if r.status == "cancelled")

    summary_table = Table(show_header=False, box=None, padding=(0, 2))
    summary_table.add_column("Label", style="bold")
    summary_table.add_column("Value")
    summary_table.add_row("Total Models", str(total))
    summary_table.add_row("Available", f"[green]{avail}[/green]")
    summary_table.add_row("Unavailable", f"[red]{unavail}[/red]")
    summary_table.add_row("Skipped", f"[yellow]{skipped}[/yellow]")
    summary_table.add_row("Cancelled", f"[dim]{cancelled}[/dim]")
    summary_table.add_row("Requests", str(report.requests_sent))
    console.print(summary_table)
    console.print()

    table = Table(
        title="Test Results",
        header_style="bold cyan",
        border_style="cyan",
        show_lines=True,
    )
    table.add_column("Endpoint", style="white", no_wrap=True)
    table.add_column("Protocol", style="dim", no_wrap=True)
    table.add_column("Model", style="white", overflow="fold")
    table.add_column("Status", justify="center")
    table.add_column("HTTP", justify="center", no_wrap=True)
    table.add_column("Error", justify="center")
    table.add_column("Attempts", justify="center")
    table.add_column("Latency(ms)", justify="right")
    table.add_column("Elapsed(ms)", justify="right")
    table.add_column("Reason", style="dim")

    for r in report.results:
        status_style = _status_style(r.status)
        error_style = _error_style(r.error_category)
        http_str = str(r.http_status) if r.http_status else "-"
        err_cat = r.error_category or "-"
        lat = f"{r.latency_ms:.0f}" if r.latency_ms else "-"
        elapsed = f"{r.elapsed_ms:.0f}" if r.elapsed_ms else "-"
        table.add_row(
            r.endpoint_id,
            f"{r.protocol} ({r.capability})" if r.capability != "text" else r.protocol,
            r.model,
            f"[{status_style}]{r.status}[/{status_style}]",
            http_str,
            f"[{error_style}]{err_cat}[/{error_style}]",
            str(r.attempts),
            lat,
            elapsed,
            (r.diagnostic_code or r.reason) if r.capability != "text" else r.reason,
        )
    console.print(table)


@app.command()
def version():
    """Print the installed version."""
    typer.echo(__version__)


def run(
    ctx: typer.Context,
    config: Path | None = typer.Option(None, help="Provider TOML file."),
    provider: str | None = typer.Option(None, help="Provider name in TOML."),
    endpoint: str | None = typer.Option(None, help="Endpoint id, or all (explicit opt-in)."),
    protocol: str | None = typer.Option(
        None, help="openai_chat, anthropic_messages, or openai_responses."
    ),
    base_url: str | None = typer.Option(None),
    api_key: str | None = typer.Option(
        None, help="API key directly; prefer interactive input to avoid shell history."
    ),
    api_key_env: str | None = typer.Option(None, help="Environment variable holding the API key."),
    proxy: str | None = typer.Option(None, help="SOCKS5 URL; use proxy_env for credentials."),
    proxy_env: str | None = typer.Option(None),
    ca_bundle: str | None = typer.Option(None),
    model: list[str] | None = typer.Option(None, "--model", help="Manual model id; repeatable."),
    models_file: Path | None = typer.Option(None),
    profile: str | None = typer.Option(None, help="baseline or chat-basic; no automatic fallback."),
    capability: str | None = typer.Option(
        None, help="text (default), stream, structured, or tool."
    ),
    max_tokens: str | None = typer.Option(
        None, help="Explicit positive token cap overriding the profile; may increase cost."
    ),
    concurrency: str | None = typer.Option(None),
    timeout: str | None = typer.Option(None),
    retries: str | None = typer.Option(None),
    max_requests: str | None = typer.Option(None),
    max_models: str | None = typer.Option(None),
    max_pages: str | None = typer.Option(None),
    rate_limit: str | None = typer.Option(None, help="Maximum request starts per second."),
    scan_timeout: str | None = typer.Option(None),
    include: str | None = typer.Option(None, help="Include model ids matching a regex."),
    exclude: str | None = typer.Option(None, help="Exclude model ids matching a regex."),
    out: Path | None = typer.Option(None),
    output_format: str = typer.Option(
        "json", "--format", help="File format for --out: json or csv."
    ),
    overwrite: bool = typer.Option(False, help="Explicitly replace an existing output file."),
    fail_on_unavailable: bool = typer.Option(False),
    verbose: bool = typer.Option(False, help="Show redacted progress on stderr."),
    pretty: bool = typer.Option(
        False,
        "--pretty",
        help="Print a colored summary to stdout. Use --out to save a report file.",
    ),
):
    """Discover or verify models. No real requests without a configured endpoint/key."""
    options = locals().copy()
    try:
        if output_format not in ("json", "csv"):
            raise ConfigError("--format must be json or csv")
        if output_format == "csv" and (out is None or ctx.info_name == "models"):
            raise ConfigError("CSV requires --out and scan/verify; models output remains JSON")
        if out and out.exists() and not overwrite:
            raise ConfigError("Output exists; use --overwrite to replace it")
        if out and not out.parent.is_dir():
            raise ConfigError("Output parent directory does not exist")
        parsed = load_provider(options)
        discover_only = ctx.info_name == "models"
        if not discover_only and len({e.probe.capability for e in parsed.endpoints}) != 1:
            raise ConfigError("Select one verification capability using --capability")
        if ctx.info_name == "verify":
            if (
                len(parsed.endpoints) != 1
                or not parsed.endpoints[0].models
                or len(parsed.endpoints[0].models) != 1
            ):
                raise ConfigError("verify requires exactly one endpoint and one manual --model")
            parsed = replace(parsed, policy=replace(parsed.policy, concurrency=1))
        for pattern in (include, exclude):
            if pattern:
                try:
                    re.compile(pattern)
                except re.error:
                    raise ConfigError("Invalid model filter regular expression") from None

        progress = ConsoleScanProgress(stderr=True) if verbose else None
        report = asyncio.run(
            scan(
                parsed,
                discover_only=discover_only,
                include=include,
                exclude=exclude,
                on_event=progress.event if progress else None,
            )
        )
        if progress:
            progress.finish(report, discover_only=discover_only)
        if out:
            write_report(report, out, output_format=output_format)
        if pretty:
            print_pretty_report(report)
        else:
            typer.echo(report_json(report))
    except ConfigError as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except Exception as exc:
        # Library errors can embed URLs, headers or credentials; never print their message.
        typer.echo(f"Operation failed ({type(exc).__name__}); no success claimed.", err=True)
        raise typer.Exit(1) from None
    if report.status == "cancelled":
        raise typer.Exit(130)
    if report.status != "completed":
        raise typer.Exit(1)
    if fail_on_unavailable and any(r.status == "unavailable" for r in report.results):
        raise typer.Exit(2)


@app.command()
def agent(
    config: Path | None = typer.Option(
        None, help="Optional legacy TOML registry; omit for saved local providers."
    ),
    data_dir: Path | None = typer.Option(
        None, help="Local provider data and scan history directory."
    ),
    local_data: bool = typer.Option(
        False, "--local-data", help="Use ./data in project root as data directory."
    ),
    open_browser: bool = typer.Option(False, help="Open the same-origin Web UI after readiness."),
    port: int = typer.Option(8766, min=1, max=65535),
    token_env: str | None = typer.Option(
        None, help="Environment variable for an Agent session token."
    ),
    token_file: Path | None = typer.Option(
        None, help="New token file; default .scout/agent.token."
    ),
    token: str | None = typer.Option(
        None,
        help="Explicit session token (32-256 URL-safe chars); exclusive with token-env/token-file.",
    ),
    max_running: int = typer.Option(2, min=1, max=16),
    max_queued: int = typer.Option(16, min=1, max=256),
    max_history: int = typer.Option(50, min=1, max=1000, help="Maximum retained terminal scans."),
):
    """Serve authenticated local scan tasks on 127.0.0.1 only."""
    try:
        import uvicorn

        from .agent.app import create_app
        from .agent.config import load_registry, prepare_token, remove_owned_token
        from .agent.history import HistoryStore
        from .providers import ProviderStore, default_store_path
    except ImportError:
        typer.echo('Install Agent dependencies: python -m pip install -e ".[agent]"', err=True)
        raise typer.Exit(1) from None
    token_val = None
    owned_path = None
    try:
        registry = load_registry(config) if config else {}
        store = None
        if local_data and data_dir is None:
            data_dir = Path.cwd() / "data"
        if config is None or data_dir is not None:
            store = ProviderStore(None if data_dir is None else data_dir / "providers.sqlite3")
        token_val, owned_path = prepare_token(token_env, token_file, token=token)
        application = create_app(
            registry,
            token=token_val,
            port=port,
            max_running=max_running,
            max_queued=max_queued,
            max_history=max_history,
            provider_store=store,
            history_store=HistoryStore((data_dir or default_store_path().parent) / "scans.sqlite3"),
        )
        typer.echo(
            json.dumps(
                {
                    "agent_url": f"http://127.0.0.1:{port}",
                    "token_file": str(owned_path) if owned_path else None,
                    "token": token_val,
                },
                ensure_ascii=False,
            )
        )
        if open_browser:
            from .launcher import open_when_ready

            open_when_ready(port, token_val)
        uvicorn.run(
            application,
            host="127.0.0.1",
            port=port,
            workers=1,
            access_log=False,
            proxy_headers=False,
            server_header=False,
            log_level="warning",
        )
    except ConfigError as exc:
        typer.echo(f"Agent configuration error: {exc}", err=True)
        raise typer.Exit(1) from None
    except Exception:
        typer.echo("Agent failed; exception details withheld", err=True)
        raise typer.Exit(1) from None
    finally:
        if token_val is not None:
            remove_owned_token(owned_path, token_val)


@app.command()
def desktop(
    port: int = typer.Option(8766, min=1, max=65535),
    data_dir: Path | None = typer.Option(None),
    local_data: bool = typer.Option(
        False,
        "--local-data",
        help="Use ./data in project root as data directory (ignored if --data-dir is set).",
    ),
    no_browser: bool = typer.Option(False),
    max_history: int = typer.Option(50, min=1, max=1000, help="Maximum retained terminal scans."),
):
    """Start Web UI with automatically saved local providers; no config file required."""
    import uuid

    from .providers import default_store_path

    if local_data and data_dir is None:
        data_dir = Path.cwd() / "data"
    directory = data_dir or default_store_path().parent
    token_file = directory / "sessions" / f"{uuid.uuid4().hex}.token"
    typer.echo("服务商信息自动加密保存；登录会话 Token 文件路径将在启动信息中显示。")
    agent(
        config=None,
        data_dir=data_dir,
        open_browser=not no_browser,
        port=port,
        token_env=None,
        token_file=token_file,
        token=None,
        max_running=2,
        max_queued=16,
        max_history=max_history,
    )


@app.command()
def interactive(
    data_dir: Path | None = typer.Option(None),
    local_data: bool = typer.Option(
        False,
        "--local-data",
        help="Use ./data in project root as data directory (ignored if --data-dir is set).",
    ),
):
    """Enter provider information, fetch/test models and export without editing files."""
    from .interactive import interactive_workflow

    if local_data and data_dir is None:
        data_dir = Path.cwd() / "data"
    try:
        interactive_workflow(data_dir)
    except (KeyboardInterrupt, EOFError, typer.Abort):
        raise typer.Exit(130) from None
    except Exception:
        typer.echo("无法打开本机安全存储，请检查当前系统用户与存储权限。", err=True)
        raise typer.Exit(1) from None


for command_name in ("models", "verify", "scan"):
    app.command(name=command_name)(run)
