# Publishing checklist (first push)

One-time steps before and after the first push to GitHub. Delete this file once done, or keep it
as the release checklist.

## Before the first commit

1. Replace the placeholders. Everything that needs a real value is marked `andrasKelle` or
   `<security contact email>`:
   ```bash
   grep -rn "andrasKelle\|<security contact email>" --exclude-dir=.venv --exclude-dir=.git .
   ```
   Files: `pyproject.toml`, `README.md`, `CHANGELOG.md`, `MAINTAINERS.md`, `.github/CODEOWNERS`,
   `.github/workflows/cla.yml`, `.github/ISSUE_TEMPLATE/config.yml`.
2. Make sure nothing local is in the tree: `config/keys.yaml` should contain hashes only (never
   a raw `lfg_` key), no `.env`, no `*.db`, no `demo/out/`. `git status` after `git add` must not
   list any of these; `.gitignore` covers them.
3. `make check` (ruff, mypy, pytest) is green.

## First commit and tag

```bash
git init -b main
git add .
git commit -m "feat: initial release of llm-finops-gateway"
git remote add origin git@github.com:andrasKelle/llm-finops-gateway.git
git push -u origin main
git tag -a v0.1.0 -m "v0.1.0"
git push origin v0.1.0        # triggers .github/workflows/release.yml (build + GHCR image + GitHub Release)
```

## In the GitHub repository settings

- **General**: enable Discussions (the issue template links to them), disable Wiki.
- **Branches**: protect `main`: require a pull request, require the `ci` status checks, no
  force pushes.
- **Actions**: allow GitHub Actions; the CLA workflow needs "Read and write permissions" for
  `GITHUB_TOKEN` (Settings, Actions, General, Workflow permissions) because it writes the
  signatures branch.
- **Security**: enable private vulnerability reporting, Dependabot alerts and security updates.
- **Packages**: after the first tag the GHCR image is private by default; make it public under
  the package settings if you want `docker pull ghcr.io/andrasKelle/llm-finops-gateway`.
- **About** (right sidebar): description "OpenAI-compatible LLM gateway: cost attribution,
  budgets as code, team keys, observability", topics `finops`, `llm`, `gateway`, `azure-openai`,
  `ollama`, `cost-management`, `platform-engineering`, `fastapi`.

## After the push

- Check that the CI badge in the README resolves (the workflow name is `ci`).
- Open the first issues from the roadmap so the project looks alive: evaluation replay
  (ADR-0009), generic OpenAI-compatible provider, OIDC attribution, streaming, price book sync.
  Label two or three of them `good first issue`.
- Only then publish the launch posts; the link in them must work.
