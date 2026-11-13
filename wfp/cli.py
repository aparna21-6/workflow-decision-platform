"""CLI: python -m wfp.cli <command>. State persists in wfp.db between commands."""
import argparse
import json
import sys

from .app import build_engine
from .errors import WorkflowError


def main(argv=None):
    p = argparse.ArgumentParser(prog="wfp")
    p.add_argument("--db", default="wfp.db")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("workflows")
    s = sub.add_parser("submit"); s.add_argument("workflow"); s.add_argument("file"); s.add_argument("--key")
    for c in ("show", "history", "audit", "explain", "retry"):
        x = sub.add_parser(c); x.add_argument("id")
    r = sub.add_parser("review"); r.add_argument("id"); r.add_argument("decision", choices=["APPROVED", "REJECTED"])
    r.add_argument("--reviewer", default="cli-user"); r.add_argument("--note", default="")
    sv = sub.add_parser("serve"); sv.add_argument("--port", type=int, default=8000)
    a = p.parse_args(argv)
    engine = build_engine(a.db)
    out = lambda o: print(json.dumps(o, indent=2))
    try:
        if a.cmd == "workflows":
            out(engine.registry.names())
        elif a.cmd == "submit":
            req, dup = engine.submit(a.workflow, json.load(open(a.file)), a.key)
            out({"request_id": req["id"], "status": req["status"], "stage": req["current_stage"], "duplicate": dup})
        elif a.cmd == "show":
            out(engine.store.get(a.id))
        elif a.cmd == "history":
            out(engine.store.history(a.id))
        elif a.cmd == "audit":
            out(engine.store.audit(a.id))
        elif a.cmd == "explain":
            e = engine.explain(a.id)
            print("\n".join(e["reasoning"]))
        elif a.cmd == "retry":
            out({"status": engine.retry(a.id)["status"]})
        elif a.cmd == "review":
            out({"status": engine.resolve(a.id, a.decision, a.reviewer, a.note)["status"]})
        elif a.cmd == "serve":
            from .api import serve
            serve(engine, port=a.port)
    except WorkflowError as e:
        print(f"ERROR ({type(e).__name__}): {e}", file=sys.stderr)
        if hasattr(e, "errors"):
            for x in e.errors:
                print("  -", x, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
