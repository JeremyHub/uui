"""A stand-in for Ollama that speaks its streaming chat protocol.

Point the backend at this with OLLAMA_URL and the whole app becomes testable in
milliseconds instead of a minute, and deterministic instead of whatever a 3B model felt
like writing. Nothing in the app knows it exists -- there is no test mode, no fake flag,
no seam cut into the code to make it reachable. It is the same server, talking to
something that answers like Ollama.

Replies are chosen by matching the request against registered rules, so a test can say
"when the user clicks Delete, the model returns this patch" and then assert on what the
app does with it.
"""

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeOllama:
    def __init__(self, chunk_delay=0.0):
        # A real model dribbles tokens out over seconds. Some behaviour only exists in
        # that gap -- painting before the response ends, predicting during idle time,
        # aborting a request mid-flight -- so the stub can be told to take its time.
        self.chunk_delay = chunk_delay
        self.rules = []          # (predicate, reply text)
        self.default = "#plan nothing\n#end"
        self.fail_with = None    # set to make the stub answer the way Ollama reports errors
        self.requests = []       # every chat payload received, for assertions
        self._server = None
        self._thread = None

    def on(self, match, reply):
        """Reply with `reply` when `match` (a substring or predicate) fits the prompt.

        `reply` may be a callable, for when successive turns need to differ -- a stub
        that answers identically every time makes "did the screen change?" unanswerable.
        """
        test = match if callable(match) else (lambda text, m=match: m in text)
        self.rules.append((test, reply))
        return self

    def reply_for(self, payload):
        self.requests.append(payload)
        text = "\n".join(m.get("content", "") for m in payload.get("messages", []))
        for test, reply in self.rules:
            if test(text):
                return reply(text) if callable(reply) else reply
        return self.default(text) if callable(self.default) else self.default

    @property
    def url(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_one_request(self):
                # The app aborts speculation by dropping the connection, which is the
                # point -- it frees the GPU immediately. That arrives here as a broken
                # pipe, and it is normal traffic, not a failure worth a stack trace.
                try:
                    super().handle_one_request()
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if fake.fail_with:
                    fake.requests.append(json.loads(body))
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": fake.fail_with}).encode() + b"\n")
                    return
                reply = fake.reply_for(json.loads(body))
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                # Chunked the way a real model streams, so the app's incremental parsing
                # is exercised rather than handed one tidy blob.
                for chunk in re.findall(r".{1,24}", reply, re.DOTALL):
                    if fake.chunk_delay:
                        time.sleep(fake.chunk_delay)
                    self.wfile.write(
                        json.dumps({"message": {"content": chunk}, "done": False}).encode() + b"\n"
                    )
                    self.wfile.flush()
                self.wfile.write(json.dumps({"message": {"content": ""}, "done": True}).encode() + b"\n")
                self.aborted = False

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
