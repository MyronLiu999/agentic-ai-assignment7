"""Assignment 7, Part D: the judge, its rates and its correction (checks/FORMATS.md,
section 15.10).

    python -m eval.judge prompt          # print the system prompt the judge would send
    python -m eval.judge tune            # the tuning set: verdicts/tuning/<prompt_sha>.jsonl, and its rates
    python -m eval.judge tune examples   # the same on the examples set
    python -m eval.judge test 1          # run 1 on the test set: eval/judge/verdicts/test_1.jsonl
    python -m eval.judge versions        # the test items' list and long versions: versions.jsonl
    python -m eval.judge recorded        # the agent way's recorded comments: recorded.jsonl
    python -m eval.judge report          # TPR, TNR, p, the correction and every test: LAB_07.json's judge

Every command takes --backend NAME (default: settings.yaml's provider). On a scripted
backend such as harness.scripted:PassEverything, verdicts go to eval/judge/verdicts/scripted/
and `report` prints instead of writing reports/LAB_07.json, so a plumbing test never mixes
with the real judge's files.

Your judge reads one output, its issue and what RepoMind's tools returned, and
answers with slide 36's fixed reply: JSON with a one-line "reasoning", then an
"answer", "Pass" or "Fail". Every call goes through a metered judge session, so its
tokens and cost are recorded and priced, and count against lab_token_ceiling:

    from harness.meter import judge_session
    with judge_session("test-1") as session:
        response = session.client.create(system=prompt, messages=[{"role": "user", "content": text}])
        session.trace.model_call(0, response, purpose="judge")

Write session.id on every verdict line. To test the plumbing for nothing, pass
backend="harness.scripted:PassEverything", a judge that passes everything: a coin.

The checker calls `correct`, `rates` and `bootstrap` on cases of its own, and
`correct` on a coin, so keep their names, arguments and returns.

A judge is its model, its prompt and its settings together. The system prompt is built
from prompts/judge.md, eval/judge/criterion.md and the examples prompts/judge.md names;
its hash, `prompt_sha`, is written on every verdict line, and a verdict file never mixes
two judges: a run whose prompt has changed refuses to append to a file the old one wrote.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
import sys
from pathlib import Path

from harness import ROOT
from harness.meter import judge_session

OUTPUTS = ROOT / "fixtures" / "judge" / "outputs"
VERDICTS = ROOT / "eval" / "judge" / "verdicts"
PROMPT = ROOT / "prompts" / "judge.md"
CRITERION = ROOT / "eval" / "judge" / "criterion.md"
LABELS = ROOT / "eval" / "labels" / "labels.csv"
SPLIT = ROOT / "eval" / "labels" / "split.yaml"
TRACES = ROOT / "fixtures" / "traces7"
POOL = ROOT / "fixtures" / "issues" / "pool"
REPORT = ROOT / "reports" / "LAB_07.json"
FOR_A_PERSON = ROOT / "eval" / "judge" / "for_a_person.txt"

COIN_MARGIN = 0.1
COMMENT_WRITES = frozenset({"issues_comment", "post_comment"})
TODO = "TODO(student)"


class CoinJudge(ValueError):
    """TPR + TNR is within 0.1 of 1: the judge is no better than a coin. Fix it, do not
    correct it (slide 44)."""


# -- the arithmetic the checker calls -------------------------------------------------------------

def is_coin(tpr: float, tnr: float) -> bool:
    return abs(tpr + tnr - 1) <= COIN_MARGIN


def correct(p: float, tpr: float, tnr: float) -> float:
    """The share you would pass, labelling by hand, from the judge's pass rate p on
    outputs nobody labelled: (p + TNR - 1) / (TPR + TNR - 1) (slide 44), clipped to
    0 to 1. Raises CoinJudge when TPR + TNR is within 0.1 of 1."""
    p, tpr, tnr = float(p), float(tpr), float(tnr)
    if is_coin(tpr, tnr):
        raise CoinJudge(f"TPR + TNR is {tpr + tnr:.3f}, within {COIN_MARGIN} of 1: the judge is no better "
                        "than a coin; fix it, do not correct it")
    return min(1.0, max(0.0, (p + tnr - 1) / (tpr + tnr - 1)))


def _norm(answer) -> str:
    return str(answer or "").strip().lower()


def rates(verdicts: dict[str, str], labels: dict[str, str]) -> tuple[float, float]:
    """(TPR, TNR) of the judge's verdicts against your labels, each "pass" or "fail",
    on the items both cover (slide 43): TPR is the share of the labelled passes the
    judge passed, TNR the share of the labelled fails it failed. A side with no
    labelled items scores 0.0."""
    passes = [i for i in labels if _norm(labels[i]) == "pass" and i in verdicts]
    fails = [i for i in labels if _norm(labels[i]) == "fail" and i in verdicts]
    tpr = sum(_norm(verdicts[i]) == "pass" for i in passes) / len(passes) if passes else 0.0
    tnr = sum(_norm(verdicts[i]) == "fail" for i in fails) / len(fails) if fails else 0.0
    return tpr, tnr


def bootstrap(verdicts: dict[str, str], labels: dict[str, str], p: float,
              resamples: int = 2000, seed: int = 0) -> tuple[float, float]:
    """The middle 95% of the corrected rate over resamples of the test items, drawn
    with replacement, as many as there are, each time recomputing TPR and TNR
    (slide 44). A resample on which the judge is a coin is left out, and so is one
    that drew no labelled pass or no labelled fail. (None, None) when none is left."""
    items = [i for i in labels if i in verdicts]
    if not items:
        return None, None
    rng = random.Random(seed)
    got = []
    for _ in range(resamples):
        draw = [rng.choice(items) for _ in items]
        n_pass = sum(_norm(labels[i]) == "pass" for i in draw)
        n_fail = len(draw) - n_pass
        if not n_pass or not n_fail:
            continue
        tpr = sum(_norm(labels[i]) == "pass" and _norm(verdicts[i]) == "pass" for i in draw) / n_pass
        tnr = sum(_norm(labels[i]) == "fail" and _norm(verdicts[i]) == "fail" for i in draw) / n_fail
        try:
            got.append(correct(p, tpr, tnr))
        except CoinJudge:
            continue
    if not got:
        return None, None
    got.sort()
    return got[int(0.025 * (len(got) - 1))], got[int(0.975 * (len(got) - 1))]


# -- what the judge reads -------------------------------------------------------------------------

def load_outputs(root: Path = ROOT) -> dict[str, dict]:
    """The 60 outputs to label, by ID (section 15.9)."""
    return {p.stem: json.loads(p.read_text(encoding="utf-8"))
            for p in sorted((root / "fixtures" / "judge" / "outputs").glob("*.json"))}


def load_labels(path: Path = LABELS) -> dict[str, dict]:
    """Your labels, by ID: {"label": "pass" | "fail" | "", "reason": str}."""
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as fh:
        return {row["id"].strip(): {"label": _norm(row.get("label")), "reason": (row.get("reason") or "").strip()}
                for row in csv.DictReader(fh) if (row.get("id") or "").strip()}


def labelled(labels: dict[str, dict], ids) -> dict[str, str]:
    return {i: labels[i]["label"] for i in ids if i in labels and labels[i]["label"] in ("pass", "fail")}


def load_split(path: Path = SPLIT) -> dict[str, list[str]]:
    import yaml
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {k: [str(x) for x in data.get(k) or []] for k in ("examples", "tuning", "test")}


def load_issue(number: int) -> dict:
    """An issue from fixtures/issues/pool/: its number, title and body."""
    import yaml
    text = (POOL / f"{number}.md").read_text(encoding="utf-8")  # every recorded run's issue is in the pool
    head, body = {}, text
    if text.startswith("---"):
        _, front, body = text.split("---", 2)
        head = yaml.safe_load(front) or {}
    return {"number": int(head.get("number") or number), "title": str(head.get("title") or ""),
            "body": body.strip()}


def recorded_items() -> list[dict]:
    """Every recorded run of the agent way in fixtures/traces7/ that posted a comment, as an
    output to judge: its issue, every call before its last comment with what each returned,
    and that comment (Part D, task 5). The other ways' comments cite nothing."""
    from harness.trace import read_trace
    out = []
    for path in sorted(p for p in TRACES.glob("*.jsonl") if not p.name.startswith("s27")):
        lines = read_trace(path)
        if not lines or lines[0].get("way") != "agent":
            continue
        calls = [x for x in lines if x.get("type") == "tool_call"]
        posted = [k for k, c in enumerate(calls) if c.get("name") in COMMENT_WRITES and not c.get("is_error")]
        if not posted:
            continue
        last = posted[-1]
        evidence = [{"tool": c.get("name"), "input": c.get("input"), "result": c.get("result", ""),
                     **({"is_error": True} if c.get("is_error") else {})} for c in calls[:last]]
        issue = lines[0].get("issue") or (lines[0].get("issues") or [None])[0]
        out.append({"id": lines[0].get("run_id") or path.stem, "system": lines[0].get("model"),
                    "issue": load_issue(int(issue)), "evidence": evidence,
                    "comment": str((calls[last].get("input") or {}).get("body") or "")})
    return out


def render(output: dict, comment: str | None = None) -> str:
    """One output as the judge reads it: the issue, what the tools returned, the comment."""
    issue = output["issue"]
    parts = [f"## The issue\n\n#{issue.get('number')}: {issue.get('title', '')}\n\n{issue.get('body', '')}",
             "## What RepoMind's tools returned, in order"]
    evidence = output.get("evidence") or []
    if not evidence:
        parts.append("(RepoMind made no tool call before it commented.)")
    for k, e in enumerate(evidence, 1):
        flag = " (returned an error)" if e.get("is_error") else ""
        parts.append(f"### Call {k}: {e.get('tool')}{flag}\n\nInput: {json.dumps(e.get('input'), sort_keys=True)}\n\n"
                     f"Result:\n{e.get('result', '')}")
    parts.append("## The comment RepoMind posted\n\n" + (output["comment"] if comment is None else comment))
    return "\n\n".join(parts)


# -- the prompt ----------------------------------------------------------------------------------

def _strip_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()


def prompt_examples(raw: str) -> list[str]:
    """The example IDs prompts/judge.md names on its `examples:` line."""
    found = re.search(r"^\s*examples:\s*(.*)$", raw, flags=re.M)
    if not found or "TODO" in found.group(1):
        return []
    return [x for x in re.split(r"[\s,\[\]]+", found.group(1)) if x]


def build_prompt(outputs: dict[str, dict], labels: dict[str, dict], split: dict[str, list[str]],
                 strict: bool) -> tuple[str, list[str]]:
    """The system prompt, and the problems that stop a real judge from running on it."""
    raw = PROMPT.read_text(encoding="utf-8")
    criterion_raw = CRITERION.read_text(encoding="utf-8") if CRITERION.exists() else ""
    problems = []
    if not criterion_raw.strip():
        problems.append(f"{CRITERION.relative_to(ROOT)} is missing")
    elif "TODO" in _strip_comments(criterion_raw):
        problems.append(f"{CRITERION.relative_to(ROOT)} still has TODO lines: write the criterion first")
    shown = prompt_examples(raw)
    if not shown:
        problems.append(f"{PROMPT.relative_to(ROOT)}: name a clear pass and a clear fail on its examples: line")
    blocks = []
    for i in shown:
        if i not in split["examples"]:
            problems.append(f"example {i} is not in the examples set")
            continue
        lab = labels.get(i) or {}
        if lab.get("label") not in ("pass", "fail") or not lab.get("reason"):
            problems.append(f"example {i} has no label and reason in {LABELS.relative_to(ROOT)}")
            continue
        reply = json.dumps({"reasoning": lab["reason"], "answer": lab["label"].capitalize()})
        blocks.append(f"<example id=\"{i}\">\n{render(outputs[i])}\n\n## The answer\n\n{reply}\n</example>")
    kinds = {(labels.get(i) or {}).get("label") for i in shown}
    if shown and not {"pass", "fail"} <= kinds:
        problems.append("the examples need at least one labelled pass and one labelled fail")
    text = _strip_comments(raw)
    text = text.replace("{criterion}", _strip_comments(criterion_raw) or "(no criterion written yet)")
    text = text.replace("{examples}", "\n\n".join(blocks) or "(no examples yet)")
    return text, problems if strict else []


def sha_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# -- one judgement -------------------------------------------------------------------------------

def parse_reply(text: str) -> tuple[str | None, str]:
    """(answer, reasoning) from the judge's reply; answer None when it is not the fixed reply."""
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    for found in re.finditer(r"\{", text):
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[found.start():])
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        answer = _norm(obj.get("answer"))
        if answer in ("pass", "fail"):
            return answer, " ".join(str(obj.get("reasoning") or "").split())
    return None, " ".join(text.split())[:300]


class Judge:
    def __init__(self, session, system: str):
        self.session, self.system, self.step = session, system, 0

    def __call__(self, text: str) -> dict:
        for attempt in range(2):
            response = self.session.client.create(system=self.system, messages=[{"role": "user", "content": text}])
            self.session.trace.model_call(self.step, response, purpose="judge")
            self.step += 1
            answer, reasoning = parse_reply(response.text)
            if answer is not None:
                return {"answer": answer, "reasoning": reasoning}
        # Two replies that are not the fixed reply: recorded as a fail, and flagged, never guessed as a pass.
        return {"answer": "fail", "reasoning": f"unparseable reply: {reasoning}", "unparsed": True}


# -- the commands --------------------------------------------------------------------------------

def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def run_items(name: str, items: list[tuple[str, str | None, str]], path: Path, system: str,
              backend: str | None) -> list[dict]:
    """Judge each (id, version, text) not already in `path` with this prompt; append as it goes,
    so a run a rate limit stopped resumes where it stopped."""
    sha = sha_of(system)
    done = read_jsonl(path)
    others = sorted({d.get("prompt_sha") for d in done} - {sha})
    if others:
        raise SystemExit(f"{path.relative_to(ROOT)} was written by another judge (prompt_sha {', '.join(map(str, others))}); "
                         f"this prompt is {sha}. A changed prompt is a new judge: move the old file aside first.")
    have = {(d.get("id"), d.get("version")) for d in done}
    todo = [it for it in items if (it[0], it[1]) not in have]
    if not todo:
        print(f"{path.relative_to(ROOT)}: all {len(items)} already judged by prompt {sha}")
        return done
    path.parent.mkdir(parents=True, exist_ok=True)
    with judge_session(name, backend=backend) as session:
        judge = Judge(session, system)
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            for k, (item, version, text) in enumerate(todo, 1):
                verdict = judge(text)
                line = {"id": item, **({"version": version} if version else {}), "answer": verdict["answer"],
                        "reasoning": verdict["reasoning"], "session": session.id, "model": session.model,
                        "prompt_sha": sha, **({"unparsed": True} if verdict.get("unparsed") else {})}
                fh.write(json.dumps(line) + "\n")
                fh.flush()
                done.append(line)
                print(f"  [{k}/{len(todo)}] {item}{' ' + version if version else ''}: {verdict['answer']}"
                      f"  {verdict['reasoning'][:100]}")
        totals = session.client.totals()
        print(f"session {session.id}: {totals['model_calls']} calls, {totals['total_tokens']:,} tokens, "
              f"${totals['cost_usd']:.4f}")
    return done


def _verdict_dir(backend: str | None) -> Path:
    from harness.runner import is_free
    from harness.settings import load_settings
    name = backend or load_settings(ROOT / "settings.yaml").provider or ""
    return VERDICTS / "scripted" if is_free(name) else VERDICTS


def _is_scripted(backend: str | None) -> bool:
    return _verdict_dir(backend) != VERDICTS


def _setup(args, need_all_labels: bool):
    outputs, labels, split = load_outputs(), load_labels(), load_split()
    strict = not _is_scripted(args.backend)
    system, problems = build_prompt(outputs, labels, split, strict)
    if strict and need_all_labels:
        missing = [i for i in outputs if (labels.get(i) or {}).get("label") not in ("pass", "fail")
                   or not (labels.get(i) or {}).get("reason")]
        if missing:
            problems.append(f"{len(missing)} outputs have no label and reason yet (e.g. {', '.join(missing[:3])}): "
                            "label all 60 by hand before the judge is frozen and tested")
    if problems:
        raise SystemExit("the judge will not run on a real model yet:\n  - " + "\n  - ".join(problems))
    return outputs, labels, split, system


def cmd_prompt(args) -> int:
    outputs, labels, split = load_outputs(), load_labels(), load_split()
    system, problems = build_prompt(outputs, labels, split, strict=True)
    print(system)
    print(f"\n-- prompt_sha {sha_of(system)}, {len(system):,} characters", file=sys.stderr)
    for p in problems:
        print(f"-- not ready: {p}", file=sys.stderr)
    return 0


def confusion(verdicts: dict[str, str], gold: dict[str, str]) -> str:
    tp = sum(gold[i] == "pass" and verdicts.get(i) == "pass" for i in gold)
    fn = sum(gold[i] == "pass" and verdicts.get(i) == "fail" for i in gold)
    tn = sum(gold[i] == "fail" and verdicts.get(i) == "fail" for i in gold)
    fp = sum(gold[i] == "fail" and verdicts.get(i) == "pass" for i in gold)
    tpr, tnr = rates(verdicts, gold)
    return f"TPR {tpr:.3f} ({tp}/{tp + fn}), TNR {tnr:.3f} ({tn}/{tn + fp})"


def cmd_tune(args) -> int:
    """Refine the prompt here, on the tuning (or examples) set only; never on test."""
    if args.which == "test":
        raise SystemExit("tune reads the examples or tuning set only: the test set is held out")
    outputs, labels, split, system = _setup(args, need_all_labels=False)
    ids = split[args.which]
    gold = labelled(labels, ids)
    if not _is_scripted(args.backend) and len(gold) < len(ids):
        raise SystemExit(f"label every {args.which} item first: {len(ids) - len(gold)} have no label")
    path = _verdict_dir(args.backend) / "tuning" / f"{args.which}_{sha_of(system)}.jsonl"
    done = run_items(f"tune-{args.which}", [(i, None, render(outputs[i])) for i in ids], path, system, args.backend)
    verdicts = {d["id"]: d["answer"] for d in done}
    print(f"\n{args.which}, prompt {sha_of(system)}: {confusion(verdicts, gold)}")
    for i in ids:
        if i in gold and verdicts.get(i) != gold[i]:
            print(f"  {i}: you {gold[i]} ({labels[i]['reason']}); judge {verdicts.get(i)}: "
                  f"{next((d['reasoning'] for d in done if d['id'] == i), '')[:160]}")
    return 0


def cmd_test(args) -> int:
    outputs, labels, split, system = _setup(args, need_all_labels=True)
    path = _verdict_dir(args.backend) / f"test_{args.run}.jsonl"
    run_items(f"test-{args.run}", [(i, None, render(outputs[i])) for i in split["test"]], path, system, args.backend)
    return 0


def cmd_versions(args) -> int:
    outputs, labels, split, system = _setup(args, need_all_labels=True)
    items = [(i, v, render(outputs[i], outputs[i]["versions"][v])) for i in split["test"] for v in ("list", "long")]
    run_items("versions", items, _verdict_dir(args.backend) / "versions.jsonl", system, args.backend)
    return 0


def cmd_recorded(args) -> int:
    outputs, labels, split, system = _setup(args, need_all_labels=True)
    items = [(r["id"], None, render(r)) for r in recorded_items()]
    run_items("recorded", items, _verdict_dir(args.backend) / "recorded.jsonl", system, args.backend)
    return 0


def judge_block(directory: Path = VERDICTS) -> dict:
    """LAB_07.json's judge (section 15.8), from the verdict files and your labels, on the test set only."""
    outputs, labels, split = load_outputs(), load_labels(), load_split()
    test = split["test"]
    gold = labelled(labels, test)
    runs = []
    for k in (1, 2, 3):
        got = {d["id"]: d["answer"] for d in read_jsonl(directory / f"test_{k}.jsonl")}
        if sorted(got) != sorted(test):
            raise SystemExit(f"test_{k}.jsonl judges {len(set(got) & set(test))} of the {len(test)} test items; "
                             f"run `python -m eval.judge test {k}` first")
        runs.append(got)
    v1 = runs[0]
    shas = {d.get("prompt_sha") for k in (1, 2, 3) for d in read_jsonl(directory / f"test_{k}.jsonl")}
    shas |= {d.get("prompt_sha") for name in ("versions", "recorded") for d in read_jsonl(directory / f"{name}.jsonl")}
    tpr, tnr = rates(v1, gold)
    recorded = read_jsonl(directory / "recorded.jsonl")
    p = sum(d["answer"] == "pass" for d in recorded) / len(recorded) if recorded else None
    coin = is_coin(tpr, tnr)
    corrected, interval = None, None
    if p is not None and not coin:
        corrected = correct(p, tpr, tnr)
        lo, hi = bootstrap(v1, gold, p)
        interval = [lo, hi] if lo is not None else None
    flips = [i for i in test if len({r[i] for r in runs}) > 1]
    majority = {i: "pass" if sum(r[i] == "pass" for r in runs) >= 2 else "fail" for i in test}
    m_tpr, m_tnr = rates(majority, gold)
    versions = read_jsonl(directory / "versions.jsonl")
    moved = sorted({d["id"] for d in versions if d["id"] in v1 and d["answer"] != v1[d["id"]]})
    by_system = {}
    for system in sorted({o.get("system") for o in outputs.values() if o.get("system")}):
        mine = {i: g for i, g in gold.items() if outputs[i].get("system") == system}
        s_tpr, s_tnr = rates(v1, mine)
        by_system[system] = {"tpr": round(s_tpr, 4), "tnr": round(s_tnr, 4),
                             "passes": sum(g == "pass" for g in mine.values()),
                             "fails": sum(g == "fail" for g in mine.values())}
    model = next((d.get("model") for d in read_jsonl(directory / "test_1.jsonl")), None)
    r = (lambda x: None if x is None else round(x, 4))
    return {
        "split": {k: len(v) for k, v in split.items()},
        "model": model, "prompt_sha": sorted(map(str, shas)),
        "tpr": r(tpr), "tnr": r(tnr), "p": r(p), "corrected": r(corrected),
        "interval": None if interval is None else [r(interval[0]), r(interval[1])],
        "coin": coin, "recorded": len(recorded),
        "test": {"items": len(test), "passes": sum(g == "pass" for g in gold.values()),
                 "fails": sum(g == "fail" for g in gold.values())},
        "flip_rate": r(len(flips) / len(test)), "flips": flips,
        # A majority is kept only when it beats one run on the same labels: higher TPR + TNR.
        "majority": {"tpr": r(m_tpr), "tnr": r(m_tnr), "kept": (m_tpr + m_tnr) > (tpr + tnr)},
        "moved_share": r(len(moved) / len(test)), "moved": moved,
        "by_system": by_system,
    }


def cmd_report(args) -> int:
    directory = _verdict_dir(args.backend)
    block = judge_block(directory)
    print(json.dumps(block, indent=2))
    if block["coin"]:
        print(f"\nThe judge is a coin: TPR + TNR = {block['tpr'] + block['tnr']:.3f}, within {COIN_MARGIN} of 1. "
              "The correction refuses: fix the judge, do not correct it.", file=sys.stderr)
    if directory != VERDICTS:
        print("\n(scripted verdicts: nothing written to reports/LAB_07.json or for_a_person.txt)", file=sys.stderr)
        return 0
    FOR_A_PERSON.write_text("".join(f"{i}\n" for i in block["flips"]), encoding="utf-8", newline="\n")
    report = json.loads(REPORT.read_text(encoding="utf-8")) if REPORT.exists() else {}
    report["judge"] = block
    REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"\nwrote reports/LAB_07.json's judge and {FOR_A_PERSON.relative_to(ROOT)} ({len(block['flips'])} items)",
          file=sys.stderr)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.judge", description=__doc__.split("\n\n")[0])
    parser.add_argument("--backend", default=None, help="default: settings.yaml's provider; "
                        "harness.scripted:PassEverything tests the plumbing for nothing")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prompt").set_defaults(fn=cmd_prompt)
    tune = sub.add_parser("tune")
    tune.add_argument("which", nargs="?", default="tuning", choices=("tuning", "examples", "test"))
    tune.set_defaults(fn=cmd_tune)
    test = sub.add_parser("test")
    test.add_argument("run", type=int, choices=(1, 2, 3))
    test.set_defaults(fn=cmd_test)
    sub.add_parser("versions").set_defaults(fn=cmd_versions)
    sub.add_parser("recorded").set_defaults(fn=cmd_recorded)
    sub.add_parser("report").set_defaults(fn=cmd_report)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
