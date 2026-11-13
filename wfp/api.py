"""Minimal REST API using only the standard library.

POST /workflows/{name}/requests      (optional header Idempotency-Key)
GET  /requests/{id}  | /requests/{id}/history | /requests/{id}/audit | /requests/{id}/explain
POST /requests/{id}/retry            POST /requests/{id}/review {"decision","reviewer","note"}
GET  /workflows                      POST /admin/workflows  (publish a new config version, no restart)
"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .errors import (IdempotencyConflict, IllegalTransition, InvalidConfig, InvalidRequest, NotFound,
                     PersistenceError, UnknownWorkflow)


def summarize(req, duplicate=None):
    out = {"request_id": req["id"], "workflow": req["workflow"], "workflow_version": req["workflow_version"],
           "status": req["status"], "current_stage": req["current_stage"], "updated_at": req["updated_at"]}
    if duplicate is not None:
        out["duplicate"] = duplicate
    return out


def make_handler(engine):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body):
            data = json.dumps(body, indent=2).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                raise InvalidRequest(["body must be valid JSON"])

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def _route(self, method):
            path = self.path.split("?")[0].rstrip("/")
            try:
                code, body = self._dispatch(method, path)
            except InvalidRequest as e:
                code, body = 422, {"error": "invalid_request", "details": e.errors}
            except InvalidConfig as e:
                code, body = 400, {"error": "invalid_config", "details": e.errors}
            except (NotFound, UnknownWorkflow) as e:
                code, body = 404, {"error": "not_found", "message": str(e)}
            except (IdempotencyConflict, IllegalTransition) as e:
                code, body = 409, {"error": type(e).__name__, "message": str(e)}
            except PersistenceError as e:
                code, body = 503, {"error": "persistence_error", "message": str(e)}
            except Exception as e:  # last resort: never leak a stack trace
                code, body = 500, {"error": "internal_error", "message": str(e)}
            self._send(code, body)

        def _dispatch(self, method, path):
            e = engine
            if method == "GET" and path == "/workflows":
                return 200, e.registry.names()
            if method == "POST" and path == "/admin/workflows":
                e.registry.register(self._json(), persist=True)
                return 201, e.registry.names()
            m = re.fullmatch(r"/workflows/([\w-]+)/requests", path)
            if method == "POST" and m:
                req, dup = e.submit(m.group(1), self._json(), self.headers.get("Idempotency-Key"))
                return (200 if dup else 201), summarize(req, dup)
            m = re.fullmatch(r"/requests/(\w+)(?:/(history|audit|explain|retry|review))?", path)
            if m:
                rid, sub = m.groups()
                if method == "GET" and sub is None:
                    return 200, {**summarize(e.store.get(rid)), "context": e.store.get(rid)["context"]}
                if method == "GET" and sub == "history":
                    return 200, e.store.history(rid)
                if method == "GET" and sub == "audit":
                    return 200, e.store.audit(rid)
                if method == "GET" and sub == "explain":
                    return 200, e.explain(rid)
                if method == "POST" and sub == "retry":
                    return 200, summarize(e.retry(rid))
                if method == "POST" and sub == "review":
                    b = self._json()
                    return 200, summarize(e.resolve(rid, b.get("decision"), b.get("reviewer", "unknown"), b.get("note", "")))
            raise NotFound(f"no route for {method} {path}")

    return Handler


def serve(engine, host="127.0.0.1", port=8000):
    server = ThreadingHTTPServer((host, port), make_handler(engine))
    print(f"Workflow platform listening on http://{host}:{port}")
    server.serve_forever()
