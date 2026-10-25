"""Run pytest on selected test subsets for the 25 leaf-component PRs and 31 planted bugs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET


def parse_junit(xml_path: Path) -> tuple[float, int, list[str]]:
    if not xml_path.exists():
        return 0.0, 0, []
    total_sec = 0.0
    count = 0
    failing = set()
    for tc in ET.parse(xml_path).getroot().iter("testcase"):
        count += 1
        total_sec += float(tc.get("time") or 0.0)
        if tc.find("failure") is None and tc.find("error") is None:
            continue
        f = tc.get("file")
        if not f:
            f = tc.get("classname", "").replace(".", "/")
            f = re.sub(r"/[A-Z][A-Za-z0-9_]*$", "", f) + ".py"
        failing.add(f)
    return round(total_sec, 4), count, sorted(failing)


def run_pytest(test_files: list[str], xml_out: Path) -> float:
    xml_out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-b",
        "-X",
        "dev",
        "-m",
        "pytest",
        "-qq",
        "--timeout=30",
        "-o",
        "console_output_style=count",
        "-p",
        "no:sugar",
        "-p",
        "no:cacheprovider",
        f"--junitxml={xml_out}",
        "-o",
        "junit_family=legacy",
        *test_files,
    ]
    t0 = time.monotonic()
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return round(time.monotonic() - t0, 4)


def main() -> None:
    mut_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "mutation")
    out_dir = Path(sys.argv[2] if len(sys.argv) > 2 else "selected_out")
    out_dir.mkdir(parents=True, exist_ok=True)

    prs = json.loads((mut_dir / "dv4_prs.json").read_text(encoding="utf-8"))
    mutants = json.loads((mut_dir / "mutants.json").read_text(encoding="utf-8"))

    comp_to_tests = {p["component"]: p["selected_test_files"] for p in prs}

    pr_results = []
    for p in prs:
        comp = p["component"]
        pr_num = p["pr"]
        tfiles = p["selected_test_files"]
        xml_path = out_dir / "junit" / f"junit-pr{pr_num}.xml"
        wall_s = run_pytest(tfiles, xml_path)
        pytest_s, tc_count, failing = parse_junit(xml_path)
        # Also save as baseline isolated run (id=0) for this component
        iso_zero = out_dir / "isolate" / f"junit-m0-r{pr_num}.xml"
        iso_zero.parent.mkdir(parents=True, exist_ok=True)
        if xml_path.exists():
            shutil.copy2(xml_path, iso_zero)
        rec = {
            "pr": pr_num,
            "component": comp,
            "commit_sha": p["commit_sha"],
            "selected_test_files_count": len(tfiles),
            "selected_traced_seconds": p["selected_traced_seconds"],
            "selected_runner_wall_seconds": wall_s,
            "selected_runner_pytest_seconds": pytest_s,
            "selected_testcases_ran": tc_count,
            "selected_failing_test_files": failing,
        }
        pr_results.append(rec)
        print(
            f"PR #{pr_num} [{comp}]: files={len(tfiles)} wall={wall_s:.2f}s "
            f"pytest={pytest_s:.2f}s tests={tc_count} fail={len(failing)}",
            flush=True,
        )

    mut_results = []
    for m in mutants:
        mid = m["id"]
        fpath = Path(m["file"])
        comp = m["file"].split("/")[2]
        tfiles = comp_to_tests[comp]
        orig_text = fpath.read_text(encoding="utf-8")
        lines = orig_text.split("\n")
        assert lines[m["line"] - 1] == m["old"], f"mutant {mid}: line mismatch"
        lines[m["line"] - 1] = m["new"]
        fpath.write_text("\n".join(lines), encoding="utf-8")
        try:
            xml_path = out_dir / "isolate" / f"junit-m{mid}-r1.xml"
            wall_s = run_pytest(tfiles, xml_path)
            pytest_s, tc_count, failing = parse_junit(xml_path)
        finally:
            fpath.write_text(orig_text, encoding="utf-8")
        rec = {
            "id": mid,
            "file": m["file"],
            "component": comp,
            "kind": m["kind"],
            "op": m["op"],
            "line": m["line"],
            "selected_test_files_count": len(tfiles),
            "selected_runner_wall_seconds": wall_s,
            "selected_runner_pytest_seconds": pytest_s,
            "selected_testcases_ran": tc_count,
            "killed_test_files_selected_run": failing,
        }
        mut_results.append(rec)
        print(
            f"M{mid:02d} [{m['kind']:6s}] {comp}: wall={wall_s:.2f}s "
            f"pytest={pytest_s:.2f}s killed={len(failing)} {failing}",
            flush=True,
        )

    summary = {
        "prs_count": len(pr_results),
        "prs_with_selected_failures": sum(1 for r in pr_results if r["selected_failing_test_files"]),
        "prs_selected_wall_seconds_total": round(sum(r["selected_runner_wall_seconds"] for r in pr_results), 2),
        "prs_selected_pytest_seconds_total": round(sum(r["selected_runner_pytest_seconds"] for r in pr_results), 2),
        "mutants_count": len(mut_results),
        "mutants_killed_in_selected_run": sum(1 for r in mut_results if r["killed_test_files_selected_run"]),
        "mutants_selected_wall_seconds_total": round(sum(r["selected_runner_wall_seconds"] for r in mut_results), 2),
        "mutants_selected_pytest_seconds_total": round(sum(r["selected_runner_pytest_seconds"] for r in mut_results), 2),
    }
    (out_dir / "dv4_selected.json").write_text(
        json.dumps({"summary": summary, "prs": pr_results, "mutants": mut_results}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
