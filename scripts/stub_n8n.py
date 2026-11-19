"""Stand-in for the n8n start, turn and end endpoints. For local worker tests only."""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

BEARER = os.environ.get("N8N_BEARER", "")
TURNS: dict[str, int] = {}


def turn(session: str, text: str) -> dict:
    TURNS[session] = TURNS.get(session, 0) + 1
    low = text.lower()
    if "slow" in low:
        time.sleep(4)
        return {"speak": "That took a while. Here is the slow answer.", "action": "none"}
    if "transfer" in low or "human" in low or "colleague" in low:
        return {"speak": "I will connect you to a colleague now.", "action": "handoff"}
    if "bye" in low or "that is all" in low:
        return {"speak": "Thank you for calling. Goodbye.", "action": "end"}
    return {"speak": f"Turn {TURNS[session]}. You said: {text}", "action": "none"}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.headers.get("Authorization") != f"Bearer {BEARER}":
            return self.reply(401, {"message": "unauthorized"})
        if self.path.endswith("/start"):
            out = {"sessionId": "STUB" + str(int(time.time()))[-6:], "speak": "Hello, this is the n8n test line. Say something, or say transfer or goodbye.", "language": "en", "action": "continue"}
        elif self.path.endswith("/turn"):
            out = turn(body.get("sessionId", ""), body.get("transcript") or body.get("text", ""))
        else:
            out = {}
        # Paths and actions only. Never the caller's words.
        print(time.strftime("%H:%M:%S"), self.path, out.get("action", ""), flush=True)
        self.reply(200, out)

    def reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
