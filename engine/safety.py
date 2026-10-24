"""Safety check on planted bugs (mutants): did the selection contain every test file a bug made fail?

Inputs (all files, nothing fetched):
  junit dir     junit-b<batch>-g<group>.xml from the mutation runs; batch 0 = baseline (no mutant)
  mutation dir  mutants.json [{id, file, kind: body|module, ...}] + batches.json [[ids of batch 0], [ids of batch 1], ...]
  static map    {test_file: [repo files in its import closure]} (engine/static_imports.py)
  runtime map   the traced run's map (engine/select.py load_runtime_map)
  isolate dir   optional: junit-m<id>-r<k>.xml, mutant <id> alone (0 = none) re-run k times on chosen tests

Method (knowledge/methodology.md, Safety):
  1. newly failing = failing in a batch - failing in the baseline batch (test-file level).
  2. flaky: newly failing in >= 3 batches and no mutant of any of those batches has it in its area's
     test folder (the profile's area rule; profiles without an area rule skip this heuristic and rely
     on baseline batches + no-mutant isolated re-runs) -> excluded, reported; unless isolated re-runs prove a mutant
     of those batches fails it every time while the no-mutant re-runs (id 0) pass (monorepos: a core bug
     legitimately breaks other packages' tests, outside its area).
  3. attribution, by tiers, first tier with a candidate wins:
       a. area: mutants of the batch whose area test folder holds the test file (profile area rule)
       b. maps: mutants whose file the test used at runtime or imports statically
     one candidate -> killed by it; none -> unexplained; several -> ambiguous.
  4. isolated re-runs settle ambiguity: a test failing in every re-run of mutant <id> alone is credited
     to <id>; passing in every re-run rules <id> out; one candidate left -> credited to it. Any failure
     with no mutant applied (id 0) marks the test flaky.
  5. selection per mutant = engine/select.py for the mutant's change kind (body -> body-only,
     module -> module-level; a body mutant flagged `runs_at_import` -> module-level, as the change
     classifier treats edits to functions that run at import time). A credited test file outside it is a miss.
  6. order_dependent: a failure credited to one bug whose test file passes in EVERY isolated re-run with
     that bug alone is neither a kill nor a miss (the batch run's test order or shared state caused it);
     it is listed apart with its evidence. Without isolation data the failure counts as before.
  An ambiguous failure every candidate's selection holds is "ambiguous_safe" (no miss possible
  whichever bug caused it); otherwise it stays "unresolved" and counts against the result.
"""
import collections
import glob
import json
import os
import re
import xml.etree.ElementTree as ET

from engine import select as sel

KIND = {"body": "body-only", "module": "module-level"}


def failing_files(xml_path):
    """Test files with >=1 failed/errored test case; the file comes from `file=` or the classname."""
    out = set()
    for tc in ET.parse(xml_path).getroot().iter("testcase"):
        if tc.find("failure") is None and tc.find("error") is None:
            continue
        f = tc.get("file")
        if not f:
            f = tc.get("classname", "").replace(".", "/")
            f = re.sub(r"/[A-Z][A-Za-z0-9_]*$", "", f) + ".py"  # drop a test class name
        out.add(f)
    return out


def ran_files(xml_path):
    return {tc.get("file") for tc in ET.parse(xml_path).getroot().iter("testcase") if tc.get("file")}


def batch_failures(junit_dir):
    failing = collections.defaultdict(set)
    for x in glob.glob(os.path.join(junit_dir, "**", "junit-b*-g*.xml"), recursive=True):
        failing[int(re.search(r"junit-b(\d+)-g", os.path.basename(x)).group(1))] |= failing_files(x)
    return failing


def isolate_results(isolate_dir):
    """{mutant id: {test file: [failed? per re-run]}} from junit-m<id>-r<k>.xml."""
    res = collections.defaultdict(lambda: collections.defaultdict(list))
    if not isolate_dir:
        return res
    for x in sorted(glob.glob(os.path.join(isolate_dir, "**", "junit-m*-r*.xml"), recursive=True)):
        mid = int(re.search(r"junit-m(\d+)-r", os.path.basename(x)).group(1))
        failed = failing_files(x)
        for t in ran_files(x) | failed:
            res[mid][t].append(t in failed)
    return res


def analyze(mutants, batches, failing, rules, idx, isolate=None):
    """mutants: {id: mutant}; batches: [[ids]]; failing: {batch: {test files}}. -> result dict."""
    isolate = isolate or {}
    rt_users = {}

    def area_tests(f):
        a = rules.area.name(f)
        return idx.folder(rules.area.tests_dir(a)) if a else set()

    def map_dep(t, f):
        if f not in rt_users:
            rt_users[f] = idx.used_at_runtime(f)
        return t in rt_users[f] or t in idx.importers.get(f, ())

    selections = {}
    for i, m in mutants.items():
        kind = "module-level" if m.get("runs_at_import") else KIND.get(m["kind"], "module-level")
        s, full, _ = sel.select([m["file"]], {m["file"]: kind}, rules, idx)
        selections[i] = (set(idx.test_files) if full else s, full)

    baseline = failing.get(0, set())
    new = {b: failing.get(b, set()) - baseline for b in range(1, len(batches))}

    # 2. flaky: fails in >= 3 batches, no mutant of those batches owns it through the area rule
    seen = collections.defaultdict(list)
    for b, ts in new.items():
        for t in ts:
            seen[t].append(b)
    flaky = {}
    for t, bs in seen.items():
        # the heuristic needs an area rule: without one no mutant "owns" any test, so every test killed by
        # >= 3 bugs (hub code) would be called flaky and its misses hidden (pydantic: 109 test files)
        if rules.area.rules and len(bs) >= 3 and not any(t in area_tests(mutants[i]["file"]) for b in bs for i in batches[b]):
            flaky[t] = {"batches": sorted(bs), "reason": "fails in %d batches, no mutant in its area" % len(bs)}
    # 2b. isolation overrides the heuristic: a mutant of those batches fails it in every re-run, no-mutant re-runs pass
    for t in list(flaky):
        clean = isolate.get(0, {}).get(t)
        if clean and not any(clean) and any(isolate.get(i, {}).get(t) and all(isolate[i][t])
                                            for b in flaky[t]["batches"] for i in batches[b]):
            del flaky[t]
    # 4. (part) a failure with no mutant applied in an isolated re-run -> flaky
    for t, runs in isolate.get(0, {}).items():
        if any(runs):
            flaky.setdefault(t, {"batches": sorted(seen.get(t, [])), "reason": "failed in isolated re-run with no mutant"})

    killed = collections.defaultdict(set)
    unexplained, ambiguous, resolved, order_dependent = [], [], [], []
    for b, ts in new.items():
        ids = batches[b]
        for t in sorted(ts - set(flaky)):
            cands = [i for i in ids if t in area_tests(mutants[i]["file"])] or \
                    [i for i in ids if map_dep(t, mutants[i]["file"])]
            if len(cands) > 1:
                fails_all = [i for i in cands if isolate.get(i, {}).get(t) and all(isolate[i][t])]
                passes_all = {i for i in cands if isolate.get(i, {}).get(t) and not any(isolate[i][t])}
                left = fails_all if len(fails_all) == 1 else [i for i in cands if i not in passes_all]
                if len(left) == 1:
                    resolved.append({"batch": b, "test_file": t, "candidates": cands, "mutant": left[0],
                                     "isolated_runs": {str(i): isolate[i][t] for i in cands if t in isolate.get(i, {})}})
                    cands = left
            if len(cands) == 1:
                runs = isolate.get(cands[0], {}).get(t)
                if runs and not any(runs):  # passes in every isolated re-run WITH the bug: an order effect of the batch run
                    order_dependent.append({"batch": b, "test_file": t, "mutant": cands[0], "isolated_runs": runs})
                    continue
                killed[cands[0]].add(t)
            elif not cands:
                unexplained.append({"batch": b, "test_file": t, "mutants": ids})
            else:
                safe = all(t in selections[i][0] for i in cands)
                ambiguous.append({"batch": b, "test_file": t, "mutants": cands, "safe": safe,
                                  "not_selected_by": [i for i in cands if t not in selections[i][0]]})

    per = {}
    unsafe_by_mutant = collections.defaultdict(list)
    for a in ambiguous:
        if not a["safe"]:
            for i in a["mutants"]:
                unsafe_by_mutant[i].append(a["test_file"])
    unexplained_batches = {u["batch"] for u in unexplained}
    safe_cands = {i for a in ambiguous if a["safe"] for i in a["mutants"]}
    for b, ids in enumerate(batches):
        if b == 0:
            continue
        for i in ids:
            m = mutants[i]
            s, full = selections[i]
            missed = sorted(killed[i] - s)
            unresolved = sorted(unsafe_by_mutant[i]) + sorted(u["test_file"] for u in unexplained if u["batch"] == b)
            if missed:
                status = "missed"
            elif killed[i]:
                status = "caught"
            elif unresolved or b in unexplained_batches:
                status = "unresolved"
            elif i in safe_cands:
                status = "ambiguous_safe"  # made tests fail together with another bug; all inside its selection
            else:
                status = "survived"
            per[str(i)] = {"file": m["file"], "kind": m["kind"], "op": m.get("op"), "line": m.get("line"), "batch": b,
                           "status": status, "killed_test_files": sorted(killed[i]), "missed": missed,
                           "unresolved_test_files": unresolved,
                           "selected_test_files": len(s), "selected_full": full,
                           "selected_pct": round(100.0 * len(s) / len(idx.test_files), 2)}

    st = collections.Counter(p["status"] for p in per.values())
    caught = [p for p in per.values() if p["status"] == "caught"]
    summary = {
        "mutants": len(per),
        "caught": st["caught"],
        "missed_mutants": st["missed"],
        "survived (no test file credited)": st["survived"],
        "unresolved_mutants": st["unresolved"],
        "ambiguous_safe_mutants (not counted as caught)": st["ambiguous_safe"],
        "missed_test_files_total": sum(len(p["missed"]) for p in per.values()),
        "unresolved_failures": sum(1 for a in ambiguous if not a["safe"]) + len(unexplained),
        "killing_test_files": sum(len(p["killed_test_files"]) for p in per.values()),
        "ambiguous_safe": sum(1 for a in ambiguous if a["safe"]),
        "resolved_by_isolation": len(resolved),
        "order_dependent": len(order_dependent),
        "unexplained_failures": len(unexplained),
        "flaky_excluded": sorted(flaky),
        "baseline_failing_test_files": len(baseline),
        "median_selected_pct_caught": sorted(p["selected_pct"] for p in caught)[len(caught) // 2] if caught else None,
    }
    return {"summary": summary, "mutants": per, "ambiguous": ambiguous, "resolved_by_isolation": resolved,
            "unexplained": unexplained, "order_dependent": order_dependent, "flaky": flaky, "baseline_failures": sorted(baseline)}


def run(profile, junit_dir, mutation_dir, static_path, runtime_path, isolate_dir=None):
    rules = sel.Rules(profile)
    with open(static_path) as f:
        static = json.load(f)
    idx = sel.TestIndex(static, sel.load_runtime_map(runtime_path))
    with open(os.path.join(mutation_dir, "mutants.json")) as f:
        mutants = {m["id"]: m for m in json.load(f)}
    with open(os.path.join(mutation_dir, "batches.json")) as f:
        batches = json.load(f)
    failing = batch_failures(junit_dir)
    if 0 not in failing:
        raise ValueError("no baseline junit (junit-b0-g*.xml) in %s" % junit_dir)
    return analyze(mutants, batches, failing, rules, idx, isolate_results(isolate_dir))
