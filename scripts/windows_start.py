"""Safe local launcher used by start.bat; never saves provider credentials."""

import argparse
import getpass
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from llm_model_scout.agent.config import load_registry, prepare_token, remove_owned_token
from llm_model_scout.config import ConfigError

ROOT = Path(__file__).resolve().parents[1]


def ensure_config(path: Path, template: Path):
    if path.exists():
        return False
    text = template.read_text(encoding="utf-8")
    with path.open("x", encoding="utf-8") as stream:
        stream.write(text)
    return True


def credential_environment(path: Path, environ, reader=None):
    env = dict(environ)
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        refs = []
        for provider in config.get("providers", []):
            for scope in (provider, *provider.get("endpoints", [])):
                for field in ("api_key_env", "proxy_env"):
                    ref = scope.get(field)
                    if ref is not None:
                        if not isinstance(ref, str) or not re.fullmatch(
                            r"[A-Za-z_][A-Za-z0-9_]{0,199}", ref
                        ):
                            raise ConfigError("凭据环境变量名称不符合规范。")
                        refs.append(ref)
    except (OSError, ValueError, AttributeError, TypeError):
        raise ConfigError("无法读取服务商配置，请检查 TOML 格式。") from None
    for ref in dict.fromkeys(refs):
        if env.get(ref):
            continue
        if reader is None and not sys.stdin.isatty():
            raise ConfigError("缺少凭据环境变量；请从交互控制台启动或提前设置变量。")
        value = (reader or getpass.getpass)(f"请输入 {ref}（隐藏输入，不保存到文件）: ")
        if not value:
            raise ConfigError("凭据不能为空，已取消启动。")
        env[ref] = value
    load_registry(path, env)
    return env


def port_available(port):
    with socket.socket() as sock:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def wait_ready(process, port, token, timeout=20):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ConfigError("服务启动失败，请检查控制台提示。")
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/providers", headers={"Authorization": f"Bearer {token}"}
            )
            with opener.open(request, timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(0.1)
    raise ConfigError("服务就绪检查超时，已停止本次启动。")


def stop_owned_process(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--token-file", type=Path, default=ROOT / ".scout/agent.token")
    parser.add_argument("--demo", action="store_true", help="Use synthetic localhost upstreams")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--smoke-test", action="store_true", help="Start, check and stop; no scan")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1-65535")
    if args.demo and args.config is not None:
        parser.error("--demo cannot be combined with --config")
    process = None
    token = None
    owned = None
    try:
        if not port_available(args.port):
            raise ConfigError(
                "端口已被占用。请使用已有服务，或用 --port 指定其他端口；不会结束占用进程。"
            )
        if args.token_file.exists() or args.token_file.is_symlink():
            raise ConfigError(
                "会话 Token 文件已存在。确认旧服务已关闭后再处理，或用 --token-file 指定新文件。"
            )
        env = dict(os.environ)
        if args.demo:
            command = [sys.executable, str(ROOT / "examples/web_demo.py")]
        else:
            path = args.config or ROOT / "providers.local.toml"
            if args.config is None and ensure_config(path, ROOT / "examples/openrouter.toml"):
                print(
                    "已创建不含密钥的 providers.local.toml（OpenRouter 示例），可自行修改。",
                    flush=True,
                )
            env = credential_environment(path, env)
            command = [
                sys.executable,
                "-m",
                "llm_model_scout",
                "agent",
                "--config",
                str(path.resolve()),
            ]
        token, owned = prepare_token(None, args.token_file)
        token_variable = "MODEL_SCOUT_SESSION_" + secrets.token_hex(8).upper()
        while token_variable in env:
            token_variable = "MODEL_SCOUT_SESSION_" + secrets.token_hex(8).upper()
        env[token_variable] = token
        command += ["--port", str(args.port), "--token-env", token_variable]
        print("正在启动本地 Model Scout…", flush=True)
        process = subprocess.Popen(command, cwd=ROOT, env=env)
        wait_ready(process, args.port, token)
        url = f"http://127.0.0.1:{args.port}/"
        print(f"服务已就绪：{url}", flush=True)
        print(f"登录会话 Token 文件：{owned}", flush=True)
        print("请复制文件中的 Token 到网页；不要粘贴服务商 API Key。", flush=True)
        if args.smoke_test:
            print("启动自检通过，未执行模型扫描。", flush=True)
            return 0
        if not args.no_browser and not webbrowser.open(url):
            print("浏览器未自动打开，请手动访问上面的地址。", flush=True)
        print("请保持窗口开启；按 Ctrl+C 停止本次服务。", flush=True)
        try:
            return process.wait()
        except KeyboardInterrupt:
            print("正在停止服务…", flush=True)
            # Console Ctrl+C is also delivered to the child; allow graceful cleanup first.
            try:
                process.wait(timeout=10)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                stop_owned_process(process)
            return 0
    except ConfigError as error:
        print(f"启动未完成：{error}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("已取消启动。", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"启动异常（{type(error).__name__}），未输出敏感详情。", file=sys.stderr)
        return 1
    finally:
        stop_owned_process(process)
        if token is not None:
            remove_owned_token(owned, token)


if __name__ == "__main__":
    raise SystemExit(main())
