"""Console entry point.

llm-finops-gateway serve --host 0.0.0.0 --port 8080
llm-finops-gateway keys new --team platform --id platform-copilot [--project copilot ...]
llm-finops-gateway keys hash lfg_...
llm-finops-gateway keys check config/keys.yaml
llm-finops-gateway pricing sync --region swedencentral [--deployment global] [--write]
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
    if args.role != "team":
        lines.append(f"    role: {args.role}")
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


def _pricing_sync(args: argparse.Namespace) -> None:
    from .pricing_sync import DEPLOYMENTS, format_table, sync

    if args.deployment not in DEPLOYMENTS:
        print(f"--deployment must be one of {', '.join(DEPLOYMENTS)}", file=sys.stderr)
        sys.exit(2)
    results, changes = sync(
        Path(args.path), args.region, args.deployment, write=args.write, currency=args.currency
    )
    print(format_table(results))
    drift = [r for r in results if r.status != "ok"]
    if args.write:
        print(f"\n{args.path}: {changes} value(s) rewritten" if changes else "\nnothing to write")
        unresolved = [r for r in drift if r.status in ("unmatched", "ambiguous")]
        if unresolved:
            print(f"{len(unresolved)} field(s) need attention (see table)", file=sys.stderr)
            sys.exit(1)
        return
    if drift:
        print(
            f"{len(drift)} field(s) differ from the {args.region} retail list; "
            "re-run with --write to update the file",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"\n{args.path} matches the {args.region} retail list ({args.deployment})")


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
    new.add_argument(
        "--role",
        choices=["team", "finops"],
        default="team",
        help="finops: may read every team's reports (ADR-0016)",
    )
    new.set_defaults(func=_keys_new)

    h = ksub.add_parser("hash", help="print the sha256 of a raw key")
    h.add_argument("key")
    h.set_defaults(func=_keys_hash)

    c = ksub.add_parser("check", help="validate a key file")
    c.add_argument("path", nargs="?", default="config/keys.yaml")
    c.set_defaults(func=_keys_check)

    pricing = sub.add_parser("pricing", help="price book tools")
    psub = pricing.add_subparsers(dest="pricing_command", required=True)
    s = psub.add_parser(
        "sync", help="compare the price book with the Azure Retail Prices API (ADR-0013)"
    )
    s.add_argument("--region", required=True, help="ARM region name, e.g. swedencentral")
    s.add_argument(
        "--deployment",
        default="global",
        help="deployment type for rows without `sku`: global (default), regional, datazone",
    )
    s.add_argument("--path", default="config/pricing.yaml")
    s.add_argument(
        "--currency",
        default="USD",
        help="billing currency of the retail list, e.g. EUR; --write also sets the book's currency",
    )
    s.add_argument("--write", action="store_true", help="rewrite differing prices in place")
    s.set_defaults(func=_pricing_sync)

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
