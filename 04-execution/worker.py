"""Container entrypoint. Bridge local Codex HTTP to the case-specific host UNIX socket."""
from pathlib import Path
import os
import hashlib
import json
import selectors
import socket
import socketserver
import subprocess
import sys
import threading

def install_plugin():
    def plugin_command(*args):
        r = subprocess.run(["codex", "plugin", *args, "--json"], stdout=subprocess.PIPE, check=True)
        return json.loads(r.stdout)
    plugin_command("marketplace", "add", "/catalog")
    added = plugin_command("add", "accounting@exam-local")
    enabled = plugin_command("list", "--marketplace", "exam-local")["installed"]
    if not any(row.get("enabled") and row.get("installed") and row.get("pluginId") == added["pluginId"]
               and row.get("version") == "1.0.1" for row in enabled):
        raise RuntimeError("Accounting plugin not installed and enabled")
    skill = Path(added["installedPath"]) / "skills/accounting/SKILL.md"
    digest = hashlib.sha256(skill.read_bytes()).hexdigest()
    if digest != "5c54b57f0ff726706188adad265545f5e0b43e3b6448ef175b04d215b7baa88e":
        raise RuntimeError("Installed skill content changed")
    cli_version = subprocess.check_output(["codex","--version"],text=True).strip()
    print(json.dumps({"codex_version":cli_version,"type":"pilot.plugin_ready","plugin":added["pluginId"], "version":added["version"],
                      "skill_path":str(skill), "skill_sha256":digest}),flush=True)
    return str(skill)



class Bridge(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
            upstream.connect("/relay/requests.sock")
            with selectors.DefaultSelector() as mux:
                mux.register(self.request, selectors.EVENT_READ, upstream)
                mux.register(upstream, selectors.EVENT_READ, self.request)
                while mux.get_map():
                    for key, _ in mux.select():
                        data = key.fileobj.recv(65536)
                        if data:
                            key.data.sendall(data)
                        else:
                            mux.unregister(key.fileobj)
                            try:
                                key.data.shutdown(socket.SHUT_WR)
                            except OSError:
                                pass


def main():
    task_id, model = sys.argv[1:]
    Path(os.environ["CODEX_HOME"]).mkdir(parents=True, exist_ok=True)
    skill = install_plugin()
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Bridge)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    prompt = (
        f"请使用 $accounting 完成 {task_id}。先单独执行 cat {skill} 并阅读全部内容。"
        "只使用 /exam/input 中的本题附件与任务书。按本题模板和字段说明独立作答，"
        "将所有要求的交付文件写入 /exam/out，保留原材料。证据不足按题设标记并完成其余事项。"
        "答卷不要记录系统或试验配置。"
    )
    cmd = ["codex", "exec", "--json", "--color", "never", "--ephemeral",
           "--skip-git-repo-check", "--model", model,
           "--sandbox", "danger-full-access", "-c", 'approval_policy="never"',
           "-c", 'web_search="disabled"', "-c", 'model_provider="pilot"',
           "-c", 'model_providers.pilot.name="Isolated OpenAI Responses"',
           "-c", f'model_providers.pilot.base_url="http://127.0.0.1:{port}/v1"',
           "-c", 'model_providers.pilot.wire_api="responses"',
           "-c", 'model_providers.pilot.supports_websockets=false', prompt]
    try:
        return subprocess.run(cmd, check=False).returncode
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
