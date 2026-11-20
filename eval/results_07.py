"""Assignment 7, Part B, task 1: one line per run, in results/<arm>.jsonl
(checks/FORMATS.md, section 15.4).

    python -m eval.results_07 --arm A --way planned --claim plan
    python -m eval.results_07 --arm B --way planned --switch TOOL_EXAMPLES=off --claim examples

Reads the arm's finished runs on your subset's golden tasks in runs/, checks each with your
grounded check, computes Part C's measure and the five metrics, and writes one line
per run. The helpers below find the runs and read them; the rest is yours.

A line (section 15.4):

    {"arm": "A", "way": "planned", "switches": {...}, "run_id": "...", "task": "g07",
     "stratum": "routine", "sweep": 1, "passed": true, "facts": {"present": 6, "total": 6},
     "measure": 0, "tokens": 31234, "cost_usd": 0.0412, "wall_clock_s": 18.2,
     "metrics": {"step_count": 2, "precision": 1.0, "recall": 1.0, "repeats": 0,
                 "orphans": 1, "no_tools": false, "cost_usd": 0.0412, "latency_s": 18.2}}

Part C's measure, per run, by claim (section 15.4): tokens, the run_end line's
totals.total_tokens; plan, the run's repeats; examples, its wasted calls: the lines of
its call record (runs/<run_id>.calls.jsonl) whose outcome is "rejected", "error" or
"protocol", plus the search calls whose result has "count": 0; checkpoint, its duplicate
writes: the comments beyond one, plus the open pull requests beyond one, on its issue
in its end state; split, 100 when the run passed and 0 when it did not.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from harness import LAB, ROOT
from harness import switches as switch_board
from harness.formats import UNFINISHED
from harness.trace import read_trace
from harness.tasks import end_state, load_tasks, read_subset
from harness.tracker import Tracker
from eval.grounded import check
from eval.metrics import metrics


def finished_runs(way: str, switches: dict | None = None, root: Path = ROOT,
                  scripted: bool = False) -> list[list[dict]]:
    """Every finished Assignment 7 run of `way` with these switches (unset ones at their
    defaults), on a real model, as its trace's lines: one list per run. A run is finished
    when its last line is run_end and it did not end quota or rate_limited. With
    scripted=True, the runs on a scripted model instead, to test your code for free."""
    want = {**switch_board.DEFAULTS, **(switches or {})}
    out = []
    for path in sorted((root / "runs").glob("*.jsonl")):
        if path.name.startswith("_") or path.name.count(".") > 1:
            continue
        lines = read_trace(path)
        start = lines[0] if lines else {}
        if start.get("type") != "run_start" or start.get("lab") != LAB or start.get("way") != way:
            continue
        if {**switch_board.DEFAULTS, **(start.get("switches") or {})} != want:
            continue
        free = str(start.get("backend", "")).startswith("harness.scripted:") or start.get("backend") == "never-stops"
        if free != scripted:
            continue
        ends = [r.get("terminal_reason") for r in lines if r.get("type") == "end"]
        if lines[-1].get("type") != "run_end" or set(ends) & set(UNFINISHED):
            continue
        out.append(lines)
    return out


def call_record(run_id: str, root: Path = ROOT) -> list[dict]:
    """A run's call record, runs/<run_id>.calls.jsonl, one dict per line."""
    path = root / "runs" / f"{run_id}.calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--arm", required=True, choices=["A", "B"])
    p.add_argument("--way", required=True)
    p.add_argument("--switch", action="append", default=[], metavar="NAME=VALUE")
    p.add_argument("--claim", required=True, choices=["tokens", "plan", "examples", "checkpoint", "split"])
    p.add_argument("--tasks", default="eval/golden/v1/tasks")
    p.add_argument("--subset", default="eval/subsets/claim.txt", help="only these task IDs (Part C)")
    p.add_argument("--scripted", action="store_true",
                   help="the runs on a scripted model, to test this script for free: writes "
                        "results/scripted_<arm>.jsonl, which the checker never reads")
    args = p.parse_args(argv)
    switches = {}
    for item in args.switch:
        name, sep, value = item.partition("=")
        name, value = name.lower(), value.lower()
        if not sep or name not in switch_board.DEFAULTS:
            p.error(f"unknown switch {item!r}")
        if isinstance(switch_board.DEFAULTS[name], bool):
            if value not in ("1", "0", "true", "false", "on", "off", "yes", "no"):
                p.error(f"invalid boolean switch {item!r}")
            value = value in ("1", "true", "on", "yes")
        elif value not in ({"derived", "uuid"} if name == "key_mode" else {"on", "off"}):
            p.error(f"invalid switch {item!r}")
        switches[name] = value
    tasks = {task.id: task for task in load_tasks(args.tasks)}
    subset = set(read_subset(args.subset))
    if not subset or not subset <= tasks.keys():
        p.error("subset must contain known task IDs")
    tracker = Tracker(flaky=False, readback_fail=False)
    rows = []
    for lines in finished_runs(args.way, switches, scripted=args.scripted):
        start = lines[0]
        if start.get("task") not in subset:
            continue
        task = tasks[start["task"]]
        state = end_state(tracker, start["run_id"], task)
        rows.append(result_row(lines, task, state, args.arm, args.claim,
                               call_record(start["run_id"])))
    target = ROOT / "results" / f"{'scripted_' if args.scripted else ''}{args.arm}.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    print(f"{len(rows)} runs written to {target.relative_to(ROOT)}")
    return 0


def result_row(lines, task, state, arm, claim, calls):
    """Build a result from observed state and trace; never from an agent's verdict."""
    start = lines[0]
    end = next(line for line in reversed(lines) if line.get("type") == "run_end")
    totals = end["totals"]
    checked = check(task, state)
    measured = metrics(lines, task)
    if claim == "tokens":
        measure = totals["total_tokens"]
    elif claim == "plan":
        measure = measured["repeats"]
    elif claim == "examples":
        measure = sum(call.get("outcome") in ("rejected", "error", "protocol") for call in calls)
        measure += sum(call.get("role") == "search" and bool(re.search(r'"count":\s*0\b',
                           str(call.get("result", "")))) for call in calls)
    elif claim == "checkpoint":
        measure = sum(max(0, len(state.get(n, state.get(str(n), {})).get(field, [])) - 1)
                      for n in task.issues for field in ("comments", "prs"))
    elif claim == "split":
        measure = 100 * int(checked["passed"])
    else:
        raise ValueError(f"unknown claim {claim!r}")
    return {"arm": arm, "way": start["way"],
            "switches": {**switch_board.DEFAULTS, **start.get("switches", {})},
            "run_id": start["run_id"], "task": task.id, "stratum": task.stratum,
            "sweep": start["sweep"], "passed": checked["passed"],
            "facts": {"present": sum(v == "present" for v in checked["facts"].values()),
                      "total": len(checked["facts"])}, "measure": measure,
            "tokens": totals["total_tokens"], "cost_usd": totals["cost_usd"],
            "wall_clock_s": end["elapsed_s"], "metrics": measured}


if __name__ == "__main__":
    raise SystemExit(main())
