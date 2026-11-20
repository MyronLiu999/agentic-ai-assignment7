"""Verify that both arms have all five sweeps before report generation."""

import json
import sys
import yaml
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harness.tasks import read_subset


def main():
    preflight = '--preflight' in sys.argv
    if preflight:
        spec = yaml.safe_load((ROOT / 'eval/thresholds/claim.yaml').read_text(encoding='utf-8'))
        expected_config = {'claim': 'plan', 'arm_a': {'way': 'planned', 'switches': {}},
                           'arm_b': {'way': 'agent', 'switches': {}}, 'sweeps': 5}
        if any(spec.get(key) != value for key, value in expected_config.items()):
            raise SystemExit('Threshold arm definitions do not match this final-sweep script.')
    tasks = read_subset(ROOT / 'eval/subsets/claim.txt')
    sweeps = range(1, 4 if preflight else 6)
    expected = Counter((task, sweep) for task in tasks for sweep in sweeps)
    for arm in ('A', 'B'):
        rows = [json.loads(line) for line in (ROOT / 'results' / f'{arm}.jsonl')
                .read_text(encoding='utf-8').splitlines() if line.strip()]
        if preflight:
            rows = [row for row in rows if row['sweep'] <= 3]
        actual = Counter((row['task'], row['sweep']) for row in rows)
        if actual != expected:
            raise SystemExit(f'Arm {arm}: incomplete or duplicate results. Missing: '
                             f'{dict(expected - actual)}; extra: {dict(actual - expected)}. '
                             'Rerun the final script to finish missing runs.')
        print(f'Arm {arm}: {len(rows)} finished runs, {len(sweeps)} sweeps accounted for.')


if __name__ == '__main__':
    main()
