"""Report only pilot spread and pooled cost, without comparing arm means."""

import json
import sys
from collections import Counter
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eval.stats import spread, sweeps_needed
from harness.tasks import read_subset


def summarize(root=ROOT):
    tasks = set(read_subset(root / 'eval/subsets/claim.txt'))
    expected = Counter((task, sweep) for task in tasks for sweep in (1, 2, 3))
    costs, dispersions = [], {}
    for arm in ('A', 'B'):
        path = root / 'results' / f'{arm}.jsonl'
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
        if Counter((row['task'], row['sweep']) for row in rows) != expected:
            raise ValueError(f'{arm}: expected exactly one finished run per task per pilot sweep. '
                             'Rerun the pilot command for missing runs; do not start sweep 4.')
        dispersions[arm] = spread([mean(row['measure'] for row in rows if row['sweep'] == sweep)
                                   for sweep in (1, 2, 3)])
        costs.extend(sum(row['cost_usd'] for row in rows if row['sweep'] == sweep)
                     for sweep in (1, 2, 3))
    # One extra repeat in one task moves an eight-task sweep by 1/8.
    sizing_sd = {arm: value if value else 1 / len(tasks) for arm, value in dispersions.items()}
    delta = 0.5
    needed = sweeps_needed(sizing_sd['A'], sizing_sd['B'], delta)
    result = {'tasks': len(tasks), 'sweeps_per_arm': 3, 'pilot_sd': dispersions,
              'sizing_sd': sizing_sd, 'zero_sd_fallback': 1 / len(tasks),
              'delta': delta, 'n_needed': needed, 'pooled_mean_sweep_cost_usd': mean(costs),
              'price_usd': needed * 2 * mean(costs),
              'note': 'Three-sweep SD is uncertain. No arm mean or gain has been compared. '
                      'Commit and push the threshold and prediction before sweep 4.'}
    target = root / 'reports' / 'PILOT_07.json'
    target.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, indent=2))
    return result


if __name__ == '__main__':
    summarize()
