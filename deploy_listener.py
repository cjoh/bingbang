"""
Tiny GitHub webhook listener.
Verifies HMAC-SHA256 against DEPLOY_SECRET, then on push events to the
configured branch runs: git fetch && git reset --hard origin/<branch>
followed by `docker compose up -d --build bingbang` so the game container
gets rebuilt.

Listens on :9000 — Caddy fronts it at https://bingbang.clay.ws/__deploy
"""
import hashlib
import hmac
import http.server
import json
import os
import subprocess
import threading
import time

SECRET   = os.environ.get("DEPLOY_SECRET", "").encode()
REPO_DIR = os.environ.get("REPO_DIR", "/repo")
BRANCH   = os.environ.get("GIT_BRANCH", "main")
PORT     = int(os.environ.get("PORT", "9000"))

_lock = threading.Lock()
_last_run = 0.0

def run(cmd, cwd=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()

def deploy():
    global _last_run
    with _lock:
        # rate limit: once every 5s max
        now = time.time()
        if now - _last_run < 5: return "rate-limited", 200
        _last_run = now
        steps = []
        for cmd in (
            ["git", "fetch", "origin", BRANCH],
            ["git", "reset", "--hard", f"origin/{BRANCH}"],
            ["git", "clean", "-fd"],
            ["docker", "compose", "-f", "docker-compose.yml", "up", "-d", "--build", "bingbang"],
        ):
            rc, out = run(cmd, cwd=REPO_DIR)
            steps.append(f"$ {' '.join(cmd)}\n[rc={rc}]\n{out}\n")
            if rc != 0:
                return "\n".join(steps), 500
        return "\n".join(steps), 200

class Handler(http.server.BaseHTTPRequestHandler):
    def _reply(self, code, body=""):
        body_b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body_b)))
        self.end_headers()
        self.wfile.write(body_b)

    def do_GET(self):
        if self.path == "/health":
            return self._reply(200, "ok\n")
        return self._reply(404, "")

    def do_POST(self):
        if self.path not in ("/", "/deploy"):
            return self._reply(404, "")
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(n) if n else b""
        except Exception:
            return self._reply(400, "bad body")
        sig = self.headers.get("X-Hub-Signature-256", "")
        if not SECRET:
            return self._reply(500, "server misconfigured: no secret")
        expected = "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return self._reply(401, "bad signature")
        event = self.headers.get("X-GitHub-Event", "")
        if event == "ping":
            return self._reply(200, "pong")
        if event != "push":
            return self._reply(200, f"ignored event: {event}")
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except Exception:
            return self._reply(400, "bad json")
        ref = payload.get("ref", "")
        if ref != f"refs/heads/{BRANCH}":
            return self._reply(200, f"ignored ref: {ref}")
        out, code = deploy()
        return self._reply(code, out)

    def log_message(self, fmt, *args):
        print("[deploy]", self.address_string(), fmt % args, flush=True)

if __name__ == "__main__":
    print(f"deploy listener on :{PORT}  repo={REPO_DIR}  branch={BRANCH}", flush=True)
    http.server.ThreadingHTTPServer(("", PORT), Handler).serve_forever()
