"""Verify the frozen judge inputs before making any final evaluation calls."""
import hashlib
import json
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    frozen = json.loads((root / 'eval/judge/frozen.json').read_text(encoding='utf-8'))
    changed = []
    for name, expected in frozen['files'].items():
        path = root / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            changed.append(name)
    if changed:
        raise SystemExit('Frozen judge inputs changed: ' + ', '.join(changed))
    print('Frozen judge verification passed.')


if __name__ == '__main__':
    main()
