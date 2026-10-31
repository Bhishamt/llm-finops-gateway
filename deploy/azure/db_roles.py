# ruff: noqa: E501  (SQL and URL strings)
"""One-time PostgreSQL role setup for an Azure deployment (docs/database.md).

    python deploy/azure/db_roles.py --vault <key vault name> [--resource-group <rg>] [--app lfg-gateway]

Reads the admin password from the vault (`postgres-password`) and the admin URL
(`database-url`), creates the tables if they do not exist yet, then:

* `lfg_app`: CONNECT, USAGE on schema public, SELECT/INSERT/UPDATE on the two ledger tables
  and USAGE on their sequences; the gateway runs with this role.
* `lfg_reporting`: CONNECT, USAGE, SELECT on every table in public, also for tables created
  later; Power BI and other readers use this role.

Writes `postgres-app-password`, `postgres-reporting-password`, `database-url-reporting` to the
vault, replaces `database-url` with the lfg_app URL and restarts the Container App so it picks
the new secret version up. Re-running is safe: existing roles get their password rotated and the
grants re-applied. Needs: the laptop's IP on the server firewall (Bicep `clientIp`), the
`[postgres]` extra, and `az` logged in with Key Vault Secrets Officer on the vault.
"""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
from urllib.parse import quote, urlsplit

import psycopg
from psycopg import sql

TABLES = ("cost_records", "budget_counters")


def az(*args: str) -> str:
    out = subprocess.run(["az", *args], check=True, capture_output=True, text=True)
    return out.stdout.strip()


def secret(vault: str, name: str) -> str:
    return az(
        "keyvault",
        "secret",
        "show",
        "--vault-name",
        vault,
        "--name",
        name,
        "--query",
        "value",
        "-o",
        "tsv",
    )


def set_secret(vault: str, name: str, value: str, content_type: str) -> None:
    az(
        "keyvault",
        "secret",
        "set",
        "--vault-name",
        vault,
        "--name",
        name,
        "--value",
        value,
        "--content-type",
        content_type,
        "-o",
        "none",
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", required=True)
    ap.add_argument("--resource-group", default="")
    ap.add_argument("--app", default="lfg-gateway")
    ap.add_argument("--no-restart", action="store_true", help="only set up roles and secrets")
    args = ap.parse_args()

    admin_url = secret(args.vault, "database-url")
    parts = urlsplit(admin_url.replace("postgresql+psycopg://", "postgresql://"))
    host, port, dbname = parts.hostname, parts.port or 5432, parts.path.lstrip("/")
    admin_user = parts.username
    admin_pw = secret(args.vault, "postgres-password")
    if parts.username == "lfg_app":
        print("database-url already points at lfg_app; using the admin password for lfgadmin")
        admin_user = "lfgadmin"

    app_pw = secrets.token_urlsafe(24)
    rep_pw = secrets.token_urlsafe(24)
    with psycopg.connect(
        host=host,
        port=port,
        dbname=dbname,
        user=admin_user,
        password=admin_pw,
        sslmode="require",
        connect_timeout=20,
        autocommit=True,
    ) as conn:
        # Tables are created by the admin, once; the app role never needs CREATE.
        from llm_finops_gateway.ledger import Ledger  # noqa: PLC0415 - optional import path

        Ledger(
            f"postgresql+psycopg://{admin_user}:{quote(admin_pw, safe='')}@{host}:{port}/{dbname}?sslmode=require"
        )
        for role, pw in (("lfg_app", app_pw), ("lfg_reporting", rep_pw)):
            exists = conn.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone()
            stmt = (
                "ALTER ROLE {} WITH LOGIN PASSWORD {}"
                if exists
                else "CREATE ROLE {} WITH LOGIN PASSWORD {}"
            )
            conn.execute(sql.SQL(stmt).format(sql.Identifier(role), sql.Literal(pw)))
            conn.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(dbname), sql.Identifier(role)
                )
            )
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
        for t in TABLES:
            conn.execute(
                sql.SQL("GRANT SELECT, INSERT, UPDATE ON {} TO lfg_app").format(sql.Identifier(t))
            )
        conn.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO lfg_app")
        conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO lfg_reporting")
        conn.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT SELECT ON TABLES TO lfg_reporting"
            ).format(sql.Identifier(admin_user))
        )
        conn.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT SELECT, INSERT, UPDATE ON TABLES TO lfg_app"
            ).format(sql.Identifier(admin_user))
        )
        conn.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO lfg_app"
            ).format(sql.Identifier(admin_user))
        )
        grants = conn.execute(
            "select grantee, table_name, string_agg(privilege_type, ',' order by privilege_type) "
            "from information_schema.table_privileges where table_schema='public' and grantee in ('lfg_app','lfg_reporting') "
            "group by 1,2 order by 1,2"
        ).fetchall()
    for g in grants:
        print(f"  {g[0]:14} {g[1]:16} {g[2]}")

    app_url = f"postgresql+psycopg://lfg_app:{quote(app_pw, safe='')}@{host}:{port}/{dbname}?sslmode=require"
    rep_url = f"postgresql://lfg_reporting:{quote(rep_pw, safe='')}@{host}:{port}/{dbname}?sslmode=require"
    set_secret(
        args.vault, "postgres-app-password", app_pw, "password of lfg_app (the gateway's role)"
    )
    set_secret(
        args.vault,
        "postgres-reporting-password",
        rep_pw,
        "password of lfg_reporting (read-only, Power BI)",
    )
    set_secret(
        args.vault, "database-url-reporting", rep_url, "read-only connection URL for BI tools"
    )
    set_secret(args.vault, "database-url", app_url, "SQLAlchemy URL used by the gateway (lfg_app)")
    print(
        f"vault {args.vault}: database-url now uses lfg_app; reporting URL in database-url-reporting"
    )
    if not args.no_restart and args.resource_group:
        az(
            "containerapp",
            "update",
            "-n",
            args.app,
            "-g",
            args.resource_group,
            "--set-env-vars",
            f"LFG_DB_ROLE_SWITCHED={secrets.token_hex(4)}",
            "-o",
            "none",
        )
        print(f"container app {args.app} restarted with a new revision")
    print(
        json.dumps(
            {
                "host": host,
                "database": dbname,
                "app_role": "lfg_app",
                "reporting_role": "lfg_reporting",
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print(exc.stderr, file=sys.stderr)
        sys.exit(exc.returncode)
