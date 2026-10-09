"""Safety check: did our selection contain every test that a mutant made fail?

python3 script/trace/safety.py --junit junit/ --mutation mutation/ --static static.json \
    --runtime map_testfiles.json.gz --out safety.json

junit/ holds junit-b<batch>-g<group>.xml from the mutation runs (batch 0 = baseline).
A failing test file is attributed to the mutant in its batch whose file it depends on
(runtime map, static import closure, or same integration folder). Exactly one candidate
is attributed; zero = "unexplained" (a dependency our maps don't see, the most important
signal); several = "ambiguous".

Selection per mutant follows experiments/home-assistant/phase1_select.py:
  body-level   -> runtime users of the file + that integration's test folder
  module-level -> static importers + runtime users + that integration's test folder
"""

from __future__ import annotations

import argparse
import collections
import glob
import gzip
import json
import os
import re
import xml.etree.ElementTree as ET


def integ(path: str) -> str | None:
    p = path.split("/")
    return p[2] if len(p) > 2 and p[1] == "components" else None


def failing_files(xml_path: str) -> set[str]:
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--junit", required=True)
    ap.add_argument("--mutation", required=True)
    ap.add_argument("--static", required=True)
    ap.add_argument("--runtime", required=True)
    ap.add_argument("--out", default="safety.json")
    a = ap.parse_args()

    mutants = {m["id"]: m for m in json.load(open(os.path.join(a.mutation, "mutants.json")))}
    batches = json.load(open(os.path.join(a.mutation, "batches.json")))
    static = json.load(open(a.static))  # test file -> import closure
    importers = collections.defaultdict(set)
    for t, deps in static.items():
        for d in deps:
            importers[d].add(t)
    rt = json.load(gzip.open(a.runtime, "rt"))
    runtime = collections.defaultdict(set)  # repo file -> test files
    runtime_rev = collections.defaultdict(set)  # test file -> repo files
    for key, idx in rt["file_to_test_files"].items():
        if key.startswith("repo:"):
            for i in idx:
                runtime[key[5:]].add(rt["test_files"][i])
                runtime_rev[rt["test_files"][i]].add(key[5:])
    all_tests = set(static)

    failing = collections.defaultdict(set)
    for x in glob.glob(os.path.join(a.junit, "**", "junit-b*-g*.xml"), recursive=True):
        b = int(re.search(r"junit-b(\d+)-g", x).group(1))
        failing[b] |= failing_files(x)
    baseline = failing.get(0, set())

    def selection(m):
        f, i = m["file"], integ(m["file"])
        folder = {t for t in all_tests if i and t.startswith(f"tests/components/{i}/")}
        if m["kind"] == "body":
            return runtime.get(f, set()) | folder
        return importers.get(f, set()) | runtime.get(f, set()) | folder

    def depends(t, m):
        f, i = m["file"], integ(m["file"])
        return f in runtime_rev.get(t, ()) or f in static.get(t, ()) or (i and t.startswith(f"tests/components/{i}/"))

    per = {}
    unexplained, ambiguous = [], []
    for b, ids in enumerate(batches):
        if b == 0:
            continue
        new_fail = failing.get(b, set()) - baseline
        killed = collections.defaultdict(set)
        for t in sorted(new_fail):
            cands = [i for i in ids if depends(t, mutants[i])]
            if len(cands) == 1:
                killed[cands[0]].add(t)
            elif not cands:
                unexplained.append({"batch": b, "test_file": t, "mutants": ids})
            else:
                ambiguous.append({"batch": b, "test_file": t, "mutants": cands})
        for i in ids:
            m = mutants[i]
            sel = selection(m)
            per[i] = {
                "file": m["file"], "kind": m["kind"], "op": m["op"], "line": m["line"],
                "killed_test_files": sorted(killed[i]),
                "selected_test_files": len(sel),
                "selected_pct": round(100 * len(sel) / len(all_tests), 2),
                "missed": sorted(killed[i] - sel),
            }

    killed_mutants = [p for p in per.values() if p["killed_test_files"]]
    summary = {
        "mutants": len(per),
        "killed": len(killed_mutants),
        "survived (no test failed)": len(per) - len(killed_mutants),
        "mutants_with_misses": sum(1 for p in per.values() if p["missed"]),
        "missed_test_files_total": sum(len(p["missed"]) for p in per.values()),
        "unexplained_failures": len(unexplained),
        "ambiguous_failures": len(ambiguous),
        "baseline_failing_test_files": len(baseline),
        "median_selected_pct_killed": sorted(p["selected_pct"] for p in killed_mutants)[len(killed_mutants) // 2] if killed_mutants else None,
    }
    json.dump({"summary": summary, "mutants": per, "unexplained": unexplained, "ambiguous": ambiguous,
               "baseline_failures": sorted(baseline)}, open(a.out, "w"), indent=1)
    print(json.dumps(summary, indent=2))
    for i, p in sorted(per.items()):
        print(f"{i:3d} {p['kind']:6s} {p['op']:15s} killed={len(p['killed_test_files']):3d} "
              f"sel={p['selected_pct']:5.1f}% missed={len(p['missed'])} {p['file']}")


if __name__ == "__main__":
    main()
