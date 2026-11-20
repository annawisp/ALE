"""Per-case HTTP-over-UNIX relay. Credentials stay in the host process."""
import contextlib
import http.client
import http.server
import json
import os
import socketserver
import threading
from pathlib import Path


class UnixHTTP(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def make_handler(api_key, model):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *_):
            pass

        def do_POST(self):
            if self.path != "/v1/responses":
                self.send_error(404)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size <= 0 or size > 32 * 1024 * 1024:
                    self.send_error(413)
                    return
                payload = json.loads(self.rfile.read(size))
                allowed_tools = {"function","custom","namespace","local_shell","tool_search"}
                if payload.get("model") != model or not payload.get("stream"):
                    self.send_error(400,"Model or request outside evaluation")
                    return
                # Codex may advertise built-in remote services even in an offline exam.
                # Only local worker-managed tool definitions may reach the model provider.
                payload["tools"] = [t for t in payload.get("tools", []) if t.get("type") in allowed_tools]
                payload["store"] = False
                data = json.dumps(payload,ensure_ascii=False).encode()
                conn = http.client.HTTPSConnection("api.openai.com", timeout=180)
                with contextlib.closing(conn):
                    conn.request("POST", "/v1/responses", body=data, headers={
                        "Authorization":"Bearer "+api_key,"Content-Type":"application/json",
                        "Accept":"text/event-stream","Accept-Encoding":"identity"})
                    resp = conn.getresponse()
                    self.send_response(resp.status)
                    self.send_header("Content-Type",resp.getheader("Content-Type","application/json"))
                    self.send_header("Connection","close")
                    self.end_headers()
                    while True:
                        chunk = resp.read1(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            except (OSError, ValueError, TypeError, http.client.HTTPException):
                self.close_connection = True
        def do_GET(self):
            self.send_error(404)
    return Handler


@contextlib.contextmanager
def relay(directory, api_key, model):
    sock = Path(directory)/"requests.sock"
    server = UnixHTTP(str(sock),make_handler(api_key,model))
    os.chmod(sock,0o600)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
