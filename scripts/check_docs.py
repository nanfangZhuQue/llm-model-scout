"""Check local documentation links; hook mode never logs or stores prompt contents."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    "README.md",
    "docs/README.md",
    "docs/requirements/PRD.md",
    "docs/design/architecture.md",
    "docs/user-guide/quick-start.md",
    "docs/user-guide/web-ui.md",
    "docs/user-guide/agent.md",
    "docs/user-guide/getting-started.md",
    "docs/user-guide/configuration.md",
    "docs/user-guide/reports-and-troubleshooting.md",
    "docs/development/acceptance.md",
    "docs/development/progress.md",
    "docs/development/development-guide.md",
    "docs/development/documentation-policy.md",
    "docs/development/changelog.md",
    "docs/development/provider-tests.md",
    "docs/archive/README.md",
)
REMINDER = (
    "本项目要求需求与文档同任务同步：新增/变更业务时核对docs/requirements/PRD.md、"
    "docs/design/architecture.md、docs/user-guide、docs/development/progress.md和changelog.md。"
    "按docs/development/documentation-policy.md的影响矩阵更新；未实现/未验证明确标记。"
    "没有相关变更时不必制造文档改动；历史原文在docs/archive。"
    "每次交付附推荐下一步计划，区分已确认需求与候选功能。OpenRouter自测授权见文档维护规则；凭据仅本机加密保存，禁止写入源码/日志，明文配置导出必须显式确认。"
)


def check_links(root: Path, paths: list[Path]) -> list[str]:
    root = root.resolve()
    errors = []
    for path in paths:
        text = path.read_text(encoding="utf-8-sig")
        fence = None
        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith(("```", "~~~")):
                marker = stripped[:3]
                if fence is None:
                    fence = marker
                elif fence == marker:
                    fence = None
                continue
            if fence is not None:
                continue
            for match in re.finditer(r"!?\[[^\]]*\]\(([^)]+)\)", line):
                target = match.group(1).strip()
                if target.startswith("<") and ">" in target:
                    target = target[1 : target.index(">")]
                else:
                    target = target.split(' "', 1)[0]
                parsed = urlsplit(target)
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                resolved = (path.parent / unquote(parsed.path)).resolve()
                prefix = f"{path.relative_to(root).as_posix()}:{number}"
                if not resolved.is_relative_to(root):
                    errors.append(f"{prefix}: local link escapes project")
                elif not resolved.exists():
                    errors.append(f"{prefix}: missing link target {parsed.path}")
    return errors


def check_docs(root: Path = ROOT) -> list[str]:
    errors = [
        f"Missing required document: {name}" for name in REQUIRED if not (root / name).is_file()
    ]
    paths = [root / "README.md"] if (root / "README.md").is_file() else []
    paths += [
        root / name
        for name in ("CLAUDE.md", "task_plan.md", "findings.md", "progress.md")
        if (root / name).is_file()
    ]
    paths += [
        p
        for p in (root / "docs").rglob("*.md")
        if "archive" not in p.relative_to(root / "docs").parts
    ]
    archive_index = root / "docs/archive/README.md"
    if archive_index.is_file():
        paths.append(archive_index)
    return errors + check_links(root, paths)


def hook_response(payload: dict, root: Path = ROOT) -> dict:
    event = payload.get("hook_event_name")
    if event == "UserPromptSubmit":
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": REMINDER}}
    if event == "Stop":
        errors = check_docs(root)
        if errors:
            reason = "文档检查失败，请修复后结束：\n" + "\n".join(errors[:12])
            if payload.get("stop_hook_active"):
                return {"systemMessage": reason}
            return {"decision": "block", "reason": reason}
    return {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hook", action="store_true")
    args = parser.parse_args()
    if args.hook:
        try:
            payload = json.loads(sys.stdin.read().lstrip("﻿"))
            if not isinstance(payload, dict):
                raise ValueError
            response = hook_response(payload)
        except (ValueError, OSError):
            response = {
                "systemMessage": "Documentation hook could not run; check input and local docs."
            }
        print(json.dumps(response, ensure_ascii=True))
        return 0
    errors = check_docs()
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Documentation required files and local links passed (semantic review is separate).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
