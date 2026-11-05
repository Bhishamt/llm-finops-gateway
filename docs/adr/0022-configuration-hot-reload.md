# 0022. The price book and the policy file are reloaded when they change

Date: 2026-11-05
Status: Proposed

## Context

Both files are data in git (ADR-0002, ADR-0005): a price change or a new registered project
is a pull request. After the merge the file lands on the running instance (a mounted volume,
a config map, a redeploy), and until now the gateway read it once at startup. A restart for
every budget change is a reason to postpone budget changes, which defeats the point of
keeping them small and frequent.

## Decision

- The gateway and the LiteLLM plugin watch the two files they loaded from disk. Before a
  request is evaluated, the file's modification time and size are compared with the last
  load; when they differ, the file is parsed again and the new price book or policy set is
  swapped in. `/healthz` and the response envelope report the versions in service.
- A file that fails to parse keeps the previous version in service, logs the error, and is not
  retried until it changes again. A missing file keeps the previous version too.
- The swap is a reference replacement: in-flight requests finish with the version they
  started with; the ledger, the budget counters and the rate-limit windows are untouched.
  Model routes are rebuilt when the price book changes, so a new model gets its provider.
- Objects injected into `create_app` or the plugin (tests, embedding) are not watched.

## Consequences

- A merged pull request takes effect on the next request, with the version visible in the
  response, so a change can be verified without a deploy.
- One `stat` per request per file; no file-system watchers, no threads.
- Deployments that mount a single file into a container should mount its directory instead:
  editors and tools replace files by rename, and a single-file bind mount keeps the old
  inode.
