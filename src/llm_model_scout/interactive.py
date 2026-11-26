"""Interactive workflows; credentials are entered privately and never printed."""

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path

import typer

from .cli import ConsoleScanProgress, print_pretty_report
from .config import model_ids
from .core.redact import Redactor
from .core.scanner import scan
from .domain import Capability
from .providers import ProviderStore, StoreError, definition_to_provider, export_definition
from .report import report_json, write_report

_VERIFICATION_ACTIONS: dict[str, tuple[Capability, bool]] = {
    "5": ("text", False),
    "6": ("text", True),
    "10": ("stream", False),
    "11": ("stream", True),
    "12": ("structured", False),
    "13": ("structured", True),
    "14": ("tool", False),
    "15": ("tool", True),
}


def prompt_definition(previous=None):
    previous = previous or {}
    name = typer.prompt(
        "服务商名称", default=previous.get("name", "我的服务商"), show_default=not bool(previous)
    )
    old = {e["protocol"]: e for e in previous.get("endpoints", [])}
    chat, messages, responses = "openai_chat", "anthropic_messages", "openai_responses"
    choices = {
        "1": (chat,),
        "2": (messages,),
        "3": (chat, messages),
        "4": (responses,),
        "5": (chat, responses),
        "6": (messages, responses),
        "7": (chat, messages, responses),
    }
    default_protocol = next(
        (key for key, kinds in choices.items() if set(kinds) == old.keys()), "1"
    )
    choice = typer.prompt(
        "协议（1 Chat / 2 Messages / 3 Chat+Messages / 4 Responses / "
        "5 Chat+Responses / 6 Messages+Responses / 7 全部）",
        default=default_protocol,
    )
    protocols = choices.get(choice)
    if protocols is None:
        raise ValueError("请选择 1 至 7")
    endpoints = []
    for protocol in protocols:
        existing = old.get(protocol, {})
        base_url = typer.prompt(
            f"{protocol} Base URL", default=existing.get("base_url", ""), show_default=False
        )
        key = typer.prompt(
            "API Key（修改时可回车保留）", hide_input=True, default="", show_default=False
        )
        proxy = typer.prompt(
            "代理URL（回车保留已有代理，输入 - 清除；输入不回显）",
            hide_input=True,
            default="",
            show_default=False,
        )
        endpoints.append(
            {
                "protocol": protocol,
                "base_url": base_url,
                "api_key": key or existing.get("api_key"),
                "proxy": None if proxy == "-" else proxy or existing.get("proxy"),
            }
        )
    definition = {"name": name, "endpoints": endpoints}
    definition_to_provider(definition)
    return definition


def write_configuration(definition, path, include_secrets=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(
        export_definition(definition, include_secrets), ensure_ascii=False, indent=2
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)


def interactive_workflow(data_dir=None):
    store = ProviderStore(None if data_dir is None else Path(data_dir) / "providers.sqlite3")
    selected = None
    last_report = None
    catalogue = None
    typer.echo("Model Scout 交互式控制台：服务商信息自动加密保存在本机。")
    while True:
        records = store.records()
        if selected not in records:
            selected = next(iter(records), None)
            last_report = catalogue = None
        name = "未选择"
        if selected:
            provider = definition_to_provider(records[selected]["definition"])
            name = Redactor.for_provider(provider).text(provider.name)
        typer.echo(f"\n当前：{name}")
        typer.echo("1 新增  2 选择  3 编辑  4 获取全部模型  5 测试全部  6 测试指定模型")
        typer.echo("7 导出测试报告  8 导出服务商配置  9 删除服务商  0 退出")
        typer.echo("10 流式测试全部  11 流式测试指定模型")
        typer.echo("12 结构化测试全部  13 结构化测试指定模型")
        typer.echo("14 工具调用测试全部  15 工具调用测试指定模型")
        action = typer.prompt("操作", default="0")
        if action == "0":
            return
        try:
            if action == "1":
                selected = store.put(prompt_definition())
                last_report = catalogue = None
                typer.echo("已加密保存。")
                continue
            if action == "2":
                keys = list(records)
                for index, key in enumerate(keys, 1):
                    p = definition_to_provider(records[key]["definition"])
                    typer.echo(f"{index}. {Redactor.for_provider(p).text(p.name)}")
                index = typer.prompt("服务商序号", type=int)
                if not 1 <= index <= len(keys):
                    raise ValueError("序号超出范围")
                selected = keys[index - 1]
                last_report = catalogue = None
                continue
            if selected is None:
                typer.echo("请先新增服务商。")
                continue
            record = records[selected]
            definition = record["definition"]
            provider = definition_to_provider(definition)
            if action == "3":
                store.put(prompt_definition(definition), selected, record["revision"])
                last_report = catalogue = None
                typer.echo("已更新并加密保存。")
            elif action == "4":
                progress = ConsoleScanProgress()
                catalogue = asyncio.run(scan(provider, discover_only=True, on_event=progress.event))
                progress.finish(catalogue, discover_only=True)
                for result in catalogue.discovery:
                    typer.echo(
                        f"{result.endpoint_id}: {len(result.models)} 个候选，状态 {result.status}"
                    )
                    for model in result.models:
                        typer.echo(model)
            elif action in _VERIFICATION_ACTIONS:
                capability, manual = _VERIFICATION_ACTIONS[action]
                ids = None
                if manual:
                    ids = model_ids(typer.prompt("模型ID，多个用英文逗号分隔").split(","))
                    if not ids:
                        raise ValueError("模型列表不能为空")
                provider = replace(
                    provider,
                    endpoints=tuple(
                        replace(
                            e,
                            probe=replace(e.probe, capability=capability),
                            models=ids if manual else e.models,
                        )
                        for e in provider.endpoints
                    ),
                )

                progress = ConsoleScanProgress()
                last_report = asyncio.run(scan(provider, on_event=progress.event))
                progress.finish(last_report)
                print_pretty_report(last_report)
                typer.echo(report_json(last_report))
            elif action == "7":
                if last_report is None:
                    typer.echo("请先完成测试。")
                    continue
                output_format = typer.prompt("格式 json/csv", default="json")
                path = Path(
                    typer.prompt("文件路径", default=f"exports/report-{selected}.{output_format}")
                )
                if path.exists():
                    raise ValueError("文件已存在，请换一个文件名")
                path.parent.mkdir(parents=True, exist_ok=True)
                write_report(last_report, path, output_format)
                typer.echo("报告已保存。")
            elif action == "8":
                include = typer.confirm(
                    "包含明文API Key和代理密码？仅保存到可信位置", default=False
                )
                suffix = ".private.json" if include else ".json"
                path = typer.prompt("文件路径", default=f"exports/provider-{selected}{suffix}")
                write_configuration(definition, path, include)
                typer.echo("服务商配置已保存；不要将含密钥文件上传云仓库。")
            elif action == "9":
                if typer.confirm("删除当前服务商的本机保存记录？", default=False):
                    store.delete(selected, record["revision"])
                    selected = None
            else:
                typer.echo("未知操作。")
        except (ValueError, OSError, StoreError):
            # Input/OS exception strings can contain user-supplied credentials or paths.
            typer.echo("操作未完成：请检查输入、文件是否已存在或本机安全存储是否可用。", err=True)
