# The ledger database

One database, two tables, three roles. Everything the gateway persists is here; everything it
decides with (prices, policies, key hashes) is a file in git and is not here (ADR-0002,
ADR-0005, ADR-0006).

## Engines

| Where | `LFG_DATABASE_URL` | Notes |
| --- | --- | --- |
| Laptop, demo | `sqlite:///./gateway.db` (default) | one file, one process |
| Azure reference deployment | `postgresql+psycopg://lfg_app:...@<server>:5432/lfg?sslmode=require` | Flexible Server, several replicas, readable by BI tools |
| Anything else SQLAlchemy speaks | any URL | PostgreSQL is the only engine the test suite runs against besides SQLite (`tests/test_ledger_postgres.py`) |

The driver for PostgreSQL comes with the `[postgres]` extra and is in the Docker image.

## Schema

Created by SQLAlchemy `create_all` on the first start (`ledger.py`); nothing else creates or
alters tables. Adding a column later means an `ALTER TABLE` by hand, as the test plan did for
`cached_input_tokens`. An existing deployment adds `run_id` with
`ALTER TABLE cost_records ADD COLUMN run_id varchar(128);` before the gateway that writes it
starts; rows written before it stay null.

### `cost_records`: one row per completed call

| Column | Type | Meaning |
| --- | --- | --- |
| `id` | integer, PK | |
| `ts` | timestamptz | when the call completed, UTC (index) |
| `request_id` | varchar(40), unique | the `id` returned to the client, `gw-...` |
| `team`, `project` | varchar(64) | attribution (indexes) |
| `use_case` | varchar(64) | optional dimension, `unspecified` when absent |
| `key_id` | varchar(64), nullable | which team key made the call |
| `run_id` | varchar(128), nullable | the caller's `X-Run-Id`, grouping the calls of one agent or pipeline run |
| `provider` | varchar(32) | adapter that served it (`azure_openai`, `openai_compat`, `ollama`) |
| `model` | varchar(64) | the price book model name (index) |
| `input_tokens`, `output_tokens` | integer | as reported by the provider |
| `cached_input_tokens` | integer | part of `input_tokens` served from the prompt cache |
| `cost_usd` | numeric(14,8) | exact decimal in the price book's currency (name kept until 1.0) |
| `price_book_version` | varchar(32) | the version the row was priced with |

No prompt or completion text, no headers, no client IP.

### `budget_counters`: month-to-date per team

| Column | Type | Meaning |
| --- | --- | --- |
| `team`, `month` | varchar, PK | `month` is `YYYY-MM` in UTC |
| `spent_usd` | numeric(14,8) | sum of recorded cost this month |
| `reserved_usd` | numeric(14,8) | estimates reserved by calls in flight (ADR-0007) |

Enforcement reads and updates this row with one conditional `UPDATE`; reports read
`cost_records`. The two are updated in the same transaction when a call is recorded.

## Roles on Azure (set up once by `deploy/azure/db_roles.py`)

| Role | Grants | Used by |
| --- | --- | --- |
| `lfgadmin` | server admin, owns the tables | the Bicep deployment, `db_roles.py`, schema changes |
| `lfg_app` | CONNECT, USAGE on `public`, SELECT/INSERT/UPDATE on the two tables, USAGE on their sequences; the same on tables created later | the gateway (`database-url` in Key Vault) |
| `lfg_reporting` | CONNECT, USAGE, SELECT on every table, also future ones | BI tools, spreadsheets, `ledger reconcile` from a laptop, anyone who reads (`database-url-reporting` in Key Vault) |

The gateway cannot DELETE or TRUNCATE; the reporting role cannot write. The script also
rotates the two passwords when re-run and restarts the Container App so it picks up the new
`database-url`. Key Vault holds: `postgres-password` (admin), `postgres-app-password`,
`postgres-reporting-password`, `database-url`, `database-url-reporting`.

Connecting from a BI tool or psql: host and database from `database-url-reporting`, user
`lfg_reporting`, SSL required; the server's firewall must list the client's IP (Bicep
`clientIp`) or the client must come through the Azure-services rule.

## Operations

- **Backups**: Flexible Server keeps 7 days of automatic backups (Bicep); SQLite is a file.
- **Retention**: nothing is deleted by the gateway. Archive or prune `cost_records` by `ts` with
  the admin role when the retention policy says so; `budget_counters` rows are one per team
  and month and stay small.
- **Reconciliation**: `llm-finops-gateway ledger reconcile` reads `cost_records` for one UTC
  day and compares it with Azure Cost Management (ADR-0018); run it with the reporting URL.
- **Reports**: `/finops`, `/v1/finops/summary(.csv)` and `/v1/finops/budgets` are served by the
  gateway from the same tables with the app role; a BI tool reads the tables directly with the
  reporting role.
