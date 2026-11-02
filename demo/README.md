# Demo

`run_demo.py` sends a weighted mix of calls from five teams through the gateway and reports
what happened to each: served, served with a budget warning, blocked by budget, blocked by
allow-list, rejected for an unpriced model, rejected for a missing owner.

```bash
pip install -e ".[demo]"
python demo/run_demo.py                      # in-process, simulated provider, no keys
python demo/run_demo.py --calls 1000 --seed 7
python demo/run_demo.py --mode live --url http://localhost:8080 --model gpt-4.1-mini --calls 30
```

Outputs in `demo/out/` (git-ignored): `report.md`, `summary.json`, `chargeback.png`.

`policies.yaml` here has budgets of a few cents so that 300 simulated calls hit every band.
The in-process mode uses the repository price book and a temporary SQLite file; nothing is
written outside `demo/out/`.
