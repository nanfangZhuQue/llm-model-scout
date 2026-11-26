"""Local-only synthetic Chat, Responses, and Anthropic endpoints."""

import argparse
import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, status, body):
        payload = json.dumps(body).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_GET(self):
        if self.path.endswith("/models"):
            self.respond(200, {"data": [{"id": "mock-ok"}, {"id": "mock-missing"}]})
        else:
            self.respond(404, {"error": {"message": "Unknown mock path"}})

    def wait_for_model(self, model):
        if model == "mock-slow":
            time.sleep(5)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length < 65536:
                raise ValueError
            body = json.loads(self.rfile.read(length))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except (ValueError, UnicodeError):
            self.respond(400, {"error": {"message": "Invalid mock request"}})
            return
        self.handle_model(body)

    def handle_model(self, body):
        self.wait_for_model(body.get("model"))
        if body.get("model") == "mock-missing":
            self.respond(
                404, {"error": {"code": "model_not_found", "message": "Mock model not found"}}
            )
        elif body.get("stream") is True and self.path.endswith(
            ("/chat/completions", "/messages", "/responses")
        ):
            self.respond_stream(body["model"])
        elif "tools" in body and self.path.endswith(
            ("/chat/completions", "/messages", "/responses")
        ):
            self.respond(200, tool_payload(self.path, body["model"]))
        elif self.path.endswith("/chat/completions") and "response_format" in body:
            self.respond_structured(body["model"], "chat")
        elif self.path.endswith("/responses") and "text" in body:
            self.respond_structured(body["model"], "responses")
        elif self.path.endswith("/messages") and "output_config" in body:
            self.respond_structured(body["model"], "messages")
        elif self.path.endswith("/chat/completions"):
            self.respond(
                200, {"choices": [{"message": {"role": "assistant", "content": "MOCK pong"}}]}
            )
        elif self.path.endswith("/responses"):
            self.respond(200, responses_payload(body.get("model")))
        elif self.path.endswith("/messages"):
            self.respond(
                200,
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "MOCK pong"}],
                },
            )
        else:
            self.respond(404, {"error": {"message": "Unknown mock path"}})

    def respond_structured(self, model, protocol):
        content = {
            "mock-structured-wrong-type": '{"status":"ok","count":"1"}',
            "mock-structured-invalid": '{"status":"ok","count":synthetic-private-structured-text}',
        }.get(model, '{"status":"ok","count":1}')
        if protocol == "chat":
            response = {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ]
            }
        elif protocol == "responses":
            response = responses_payload("mock-ok")
            response["output"][0]["content"][0]["text"] = content
        else:
            response = {
                "type": "message",
                "role": "assistant",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": content}],
            }
        self.respond(200, response)

    def before_stream_terminal(self, model):
        pass

    def respond_stream(self, model):
        initial, terminal = stream_frames(
            self.path, " " if model == "mock-stream-empty" else "合成文本"
        )

        def write_chunk(data):
            self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()

        self.protocol_version = "HTTP/1.1"
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            for frame in initial:
                write_chunk(frame)
            self.before_stream_terminal(model)
            if model == "mock-stream-abrupt":
                return  # Omit the final HTTP chunk to simulate a broken connection.
            if model == "mock-stream-limit":
                comment = b": " + b"x" * 8192 + b"\n\n"
                for _ in range(257):
                    write_chunk(comment)
            if model != "mock-stream-eof":
                for frame in terminal:
                    write_chunk(frame)
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


def stream_frames(path, content):
    def frame(payload, event=None):
        prefix = f"event: {event}\n" if event else ""
        return (prefix + "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()

    def named(kind, **fields):
        return frame({"type": kind, **fields}, kind)

    if path.endswith("/chat/completions"):

        def chat(delta, finish=None):
            return frame(
                {
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
            )

        return (
            [chat({"role": "assistant"}), chat({"content": content})],
            [chat({}, "stop"), b"data: [DONE]\n\n"],
        )
    if path.endswith("/messages"):
        return (
            [
                named(
                    "message_start", message={"type": "message", "role": "assistant", "content": []}
                ),
                named("content_block_start", index=0, content_block={"type": "text", "text": ""}),
                named(
                    "content_block_delta", index=0, delta={"type": "text_delta", "text": content}
                ),
            ],
            [
                named("content_block_stop", index=0),
                named("message_delta", delta={"stop_reason": "end_turn", "stop_sequence": None}),
                named("message_stop"),
            ],
        )

    response = responses_payload("mock-ok")
    message = response["output"][0]
    part = message["content"][0]
    part["text"] = content
    message["id"] = "mock_message"
    response["id"] = "mock_response"
    indices = {"item_id": "mock_message", "output_index": 0, "content_index": 0}
    return (
        [
            named("response.created", response=response | {"status": "in_progress", "output": []}),
            named(
                "response.output_item.added",
                output_index=0,
                item=message | {"status": "in_progress", "content": []},
            ),
            named("response.content_part.added", **indices, part=part | {"text": ""}),
            named("response.output_text.delta", **indices, delta=content),
        ],
        [
            named("response.output_text.done", **indices, text=content),
            named("response.content_part.done", **indices, part=part),
            named("response.output_item.done", output_index=0, item=message),
            named("response.completed", response=response),
        ],
    )


def tool_payload(path, model):
    tool_name = (
        "synthetic_private_tool_name" if model == "mock-tool-wrong-name" else "scout_tool_probe"
    )
    arguments = {
        "value": "ping",
        "count": "synthetic-private-tool-argument" if model == "mock-tool-wrong-args" else 1,
    }
    count = 0 if model == "mock-tool-none" else 2 if model == "mock-tool-multiple" else 1
    calls = [(f"synthetic_private_call_{index}", tool_name, arguments) for index in range(count)]
    text = "synthetic-private-tool-text"

    if path.endswith("/chat/completions"):
        message = {"role": "assistant", "content": text if not calls else None}
        if calls:
            message["tool_calls"] = [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
                for call_id, name, args in calls
            ]
        return {
            "choices": [
                {"index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop"}
            ]
        }

    if path.endswith("/messages"):
        return {
            "type": "message",
            "role": "assistant",
            "stop_reason": "tool_use" if calls else "end_turn",
            "content": [
                {"type": "tool_use", "id": call_id, "name": name, "input": args}
                for call_id, name, args in calls
            ]
            if calls
            else [{"type": "text", "text": text}],
        }

    response = responses_payload("mock-ok")
    if calls:
        response["output"] = [
            {
                "type": "function_call",
                "id": f"fc_{index}",
                "call_id": call_id,
                "status": "completed",
                "name": name,
                "arguments": json.dumps(args),
            }
            for index, (call_id, name, args) in enumerate(calls)
        ]
    else:
        response["output"][0]["content"][0]["text"] = text
    return response


def responses_payload(model):
    response = {"object": "response", "status": "completed", "error": None}
    content = {"type": "output_text", "text": "MOCK pong", "annotations": []}
    if model == "mock-responses-refusal":
        content = {"type": "refusal", "refusal": "Synthetic refusal"}
    elif model == "mock-responses-empty":
        content["text"] = " \n"
    elif model == "mock-responses-incomplete":
        response.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    elif model == "mock-responses-error":
        return response | {
            "status": "failed",
            "error": {"code": "server_error", "message": "Synthetic error"},
            "output": [],
        }
    return response | {
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "status": response["status"],
                "content": [content],
            }
        ]
    }


@contextmanager
def running_mock(port=0, *, handler=MockHandler):
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    with running_mock(args.port) as base:
        print(f"Synthetic mock only: {base}/v1", flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
