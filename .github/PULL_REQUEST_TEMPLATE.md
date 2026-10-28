## What

<!-- One or two sentences. Link the issue: Closes #123 -->

## Why

<!-- The problem this solves. If an ADR applies, link it. -->

## How

<!-- Notable implementation choices, trade-offs, anything a reviewer should look at first. -->

## Checklist

- [ ] `ruff check . && ruff format --check .` passes
- [ ] `mypy src tests` passes (strict)
- [ ] `pytest -q` passes; new behaviour has tests
- [ ] `CHANGELOG.md` updated under *Unreleased*
- [ ] ADR added or updated if this changes an interface, schema, default or cost semantics
- [ ] No secrets, personal data or real prompts in code, tests or fixtures
- [ ] I have signed the CLA (first contribution only)
