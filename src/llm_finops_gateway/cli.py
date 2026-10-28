"""Console entry point.

llm-finops-gateway serve --host 0.0.0.0 --port 8080
llm-finops-gateway keys new --team platform --id platform-copilot [--project copilot ...]
llm-finops-gateway keys hash lfg_...
llm-finops-gateway keys check config/keys.yaml
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from .auth import KeyRing, generate_key, hash_key


def _serve(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run(
        "llm_finops_gateway.main:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


def _keys_new(args: argparse.Namespace) -> None:
    raw, digest = generate_key()
    today = datetime.now(UTC).date().isoformat()
    lines = [
        f"  - id: {args.id}",
        f"    team: {args.team}",
        f"    sha256: {digest}",
        f"    created: {today}",
    ]
    if args.project:
        lines.append(f"    projects: [{', '.join(args.project)}]")
    if args.expires:
        lines.append(f"    expires: {args.expires}")
    print("# Give this key to the team. It is shown once and never stored by the gateway:")
    print(raw)
    print()
    print("# Append this entry under `keys:` in config/keys.yaml (hash only, safe to commit).")
    print("# If the file still says `keys: []`, replace that line with `keys:` first.")
    print("\n".join(lines))
    print()
    print("# Then: llm-finops-gateway keys check")


def _keys_hash(args: argparse.Namespace) -> None:
    print(hash_key(args.key))


def _keys_check(args: argparse.Namespace) -> None:
    ring = KeyRing.from_yaml(Path(args.path))
    print(f"{args.path}: {len(ring)} key(s), version {ring.version}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-finops-gateway")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the gateway (default)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=_serve)

    keys = sub.add_parser("keys", help="manage team API keys")
    ksub = keys.add_subparsers(dest="keys_command", required=True)

    new = ksub.add_parser("new", help="generate a key for a team")
    new.add_argument("--team", required=True)
    new.add_argument("--id", required=True, help="label for the key, e.g. platform-copilot")
    new.add_argument("--project", action="append", help="restrict to project (repeatable)")
    new.add_argument("--expires", help="YYYY-MM-DD")
    new.set_defaults(func=_keys_new)

    h = ksub.add_parser("hash", help="print the sha256 of a raw key")
    h.add_argument("key")
    h.set_defaults(func=_keys_hash)

    c = ksub.add_parser("check", help="validate a key file")
    c.add_argument("path", nargs="?", default="config/keys.yaml")
    c.set_defaults(func=_keys_check)

    return parser


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Backwards compatible: bare `llm-finops-gateway --port 8080` means `serve`.
    if not argv or argv[0].startswith("-"):
        argv = ["serve", *argv]
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
