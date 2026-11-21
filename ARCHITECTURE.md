# ARCHITECTURE.md · RepoMind on the seven layers

For each layer: what does this job, and where is it enforced. Cite code as a file
path and line number joined by a colon, inside backticks; a checker opens every
reference (`checks/FORMATS.md`, section 8). A layer held only by a sentence in a
prompt (any file under `prompts/`) is **EMPTY**, and so is a layer nothing does
yet. Status is one of: present, partial (say what is missing), or **EMPTY**. Each
assignment brings this file up to date.

What each layer is for (Lecture 1, slides 42 to 44):

- **Model**: reasoning, choosing tools, generating text.
- **Harness**: runs the loop, builds each prompt, trims old history.
- **Tools**: where the system acts on the world.
- **Memory & State**: what survives a step, a session, a restart.
- **Control & Policy**: what the system may do, and when it must ask.
- **Observability**: a step-by-step record of what it did.
- **Evaluation**: measuring quality, before and after release.

The table below is RepoMind as Assignment 5 leaves it: its working solution.

| Layer | What does this job | Where it is enforced | Status |
|---|---|---|---|
| Model | The configured model, called only through the metered client | `harness/client.py:150` | present |
| Harness | The loop, with three bounds checked in code; a plan validated before it runs, and an executor that runs only the plan's steps; the client drains the user's messages before every call | `agent/loop.py:38`, `ways/planned.py:327`, `ways/planned.py:488`, `ways/planned.py:180` | present |
| Tools | Ten namespaced tools behind one registry: every call checked at the boundary before it runs; every write keyed by the harness, checked before each attempt and read back after a timeout; every attempt in the call record | `tools/__init__.py:170`, `tools/__init__.py:260`, `tools/__init__.py:269`, `tools/__init__.py:286`, `tools/__init__.py:325` | present |
| Memory & State | A checkpoint after each file read and before each write, sync, and a resume from the last one; a store of dated records with their origin, read before the first search and written once at the end of a run | `ways/planned.py:387`, `ways/planned.py:400`, `ways/planned.py:263`, `ways/planned.py:283`, `state/checkpointer.py:55`, `state/store.py:96` | present |
| Control & Policy | The autonomy policy in three columns, enforced by a pre-call hook outside the model: a stop first, prohibited before everything, a user's boundary kept as a rule, the disclosure line, and the reviewer asked for a gated call, with no answer never a yes; an escalation on a named signal that holds; a stop with a live notification; rollback by the remote's compensations | `tools/__init__.py:147`, `policy/hook.py:52`, `policy/hook.py:130`, `policy/hook.py:142`, `policy/hook.py:145`, `policy/hook.py:154`, `policy/hook.py:174`, `policy/boundary.py:33`, `ways/planned.py:209`, `ways/planned.py:460`, `ways/planned.py:546`, `policy/escalation.py:49`, `bin/rollback:24` | partial: the approvals still decay, as Lecture 5's Section B predicts, and nothing measures harm prevented |
| Observability | One trace line per model call and per tool call, and the plan as data before it runs; every refusal in the hook log, every decision in the decisions log, every write in the remote's log | `agent/loop.py:50`, `ways/planned.py:219`, `policy/hook.py:183` | partial: traces and logs, no alerts |
| Evaluation | A versioned golden set; the grounded check reads the run's end state from the tracker, never the agent; five trajectory metrics from the trace; pass@k, pass^k, spreads and intervals over sweeps; a bar frozen before the fourth sweep; a judge validated on held-out human labels, with its rate corrected and a coin refused | `eval/golden/v1/manifest.yaml:1`, `eval/grounded.py:32`, `eval/grounded.py:75`, `eval/metrics.py:60`, `eval/stats.py:22`, `eval/stats.py:64`, `eval/stats.py:74`, `eval/results_07.py:123`, `eval/thresholds/claim.yaml:1`, `eval/judge.py:80`, `eval/judge.py:95`, `eval/judge.py:107`, `eval/judge.py:229` | partial: offline only, and the judge's TNR is 6/13 |

**Empty layers: 0 of 7**, and three are partial.

## Assignment 7 evaluation

The evaluation layer now loads a versioned 30-task golden set, checks run-scoped tracker state in eval/grounded.py, counts trajectories in eval/metrics.py, and exports completed runs in eval/results_07.py. eval/stats.py supplies finite-sample pass metrics and sweep-based uncertainty. The real-model comparison is complete (80 runs; cannot_settle for repeats). The frozen judge has been validated on 26 held-out items with three runs, formatting variants, correction and coin protection; its low specificity remains a limitation. See reports/LAB_07.md for validation and environment limitations.

Where each part lives: `eval/grounded.py:32` (`check`, every fact of the expected end state) and `eval/grounded.py:75` (`check_run`, the same on a run's tracker state); `eval/metrics.py:60` (step count, precision, recall, repeats, orphans); `eval/stats.py:22`, `:29`, `:36` (pass@k, pass^k, their task average), `:44`, `:55`, `:64` (spread, margin, interval) and `:74` (sweeps needed); `eval/results_07.py:44` and `:123` (finished runs, one result row each); `eval/judge.py:80` (`correct`, with the coin guard), `:95` (`rates`), `:107` (`bootstrap`), `:229` (the prompt built from `prompts/judge.md`, `eval/judge/criterion.md` and the labelled examples), `:308` (metered sessions, one prompt per verdict file) and `:429` (`LAB_07.json`'s judge).
