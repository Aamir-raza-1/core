"""engine CLI: audit | trace | select | verify | affected-tests | index | flaky | report | shadow.

  python3 -m engine.cli audit --provider gitlab --project gitlab-org/gitlab --days 1 --end 2026-10-10
  python3 -m engine.cli select --profile profiles/<owner>__<repo>.yaml --changed a.py b.py
  python3 -m engine.cli index --profile P --static static.json --map map_testfiles.json.gz --out repo.idx
  python3 -m engine.cli affected-tests --profile P --index repo.idx --changed a.py [--explain --out decisions.json]

Exit codes: 0 ok, 1 error, 2 not implemented yet.
"""
import argparse
import json
import os
import sys

if __package__ in (None, ""):  # allow `python3 engine/cli.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import BUNDLED_PROFILES_DIR, __version__, profile  # noqa: E402

if not os.path.exists(profile.SCHEMA):
    _bundled_schema = os.path.join(BUNDLED_PROFILES_DIR, "schema.json")
    if os.path.exists(_bundled_schema):
        profile.SCHEMA = _bundled_schema
        profile.load.__defaults__ = (_bundled_schema,)


def _resolve_profile(path):
    if os.path.exists(path):
        return path
    norm = path.replace("\\", "/")
    if norm.startswith("profiles/"):
        cand = os.path.join(BUNDLED_PROFILES_DIR, os.path.basename(norm))
        if os.path.exists(cand):
            return cand
    return path

# engine.audit (and its http stack) is imported only by the commands that use it: the selection
# commands must start fast (D9: < 100 ms per decision including process start).


class _Providers:
    """argparse choices for --provider, resolved from engine.audit only when needed."""

    def _list(self):
        from engine import audit  # noqa: PLC0415
        return audit.PROVIDERS

    def __contains__(self, x):
        return x in self._list()

    def __iter__(self):
        return iter(self._list())


def _audit():
    from engine import audit  # noqa: PLC0415
    return audit


def Window(*a):  # noqa: N802
    from engine.audit.common import Window as W  # noqa: PLC0415
    return W(*a)

NOT_YET = {"trace": "B (tracer lives in the fork's script/trace until R1)"}


def cmd_audit(a):
    audit = _audit()
    try:
        s = audit.run(a.provider, a.project, Window(a.days, a.end),
                      test_job_regex=a.test_job_regex, full_run_rule=a.full_run_rule,
                      **({"workflows": a.workflow} if a.workflow else {}), **({"job": a.job} if a.job else {}))
    except NotImplementedError as e:
        print(e, file=sys.stderr)
        return 2
    out = a.out or audit.out_path(a.provider, a.project)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(s, f, indent=1)
        f.write("\n")
    d = s["duration_min"]
    print("%s %s: %d runs (%.1f/day), duration median/p90 %s/%s min (%s/%s with queue), failure %s%%, re-run %s%%, flaky commits %d -> %s"
          % (s["provider"], s["project"], s["runs"], s["runs_per_day"], d["from_started"]["median"], d["from_started"]["p90"],
             d["from_created"]["median"], d["from_created"]["p90"], s["failure_pct"], s["rerun_pct"], s["flaky_commits"], out))
    return 0


def _index(a):
    from engine import select as sel  # noqa: PLC0415
    prof_path = _resolve_profile(a.profile)
    seconds = None
    if getattr(a, "seconds", None):
        with open(a.seconds) as f:
            seconds = json.load(f)
    if a.index:
        if a.static or a.map:
            raise ValueError("--index replaces --static/--map (the maps are compiled into it)")
        from engine.index import CompiledIndex  # noqa: PLC0415
        idx = CompiledIndex(a.index, seconds)
        prof = idx.cached_profile(prof_path) or profile.load(prof_path)
        idx.map_source, idx.map_commit = idx.meta.get("map_source"), idx.meta.get("map_commit")
        return sel, sel.Rules(prof), idx
    if not a.static:
        raise ValueError("give --static (+ --map) or --index")
    rules = sel.Rules(profile.load(prof_path))
    with open(a.static) as f:
        static = json.load(f)
    runtime = sel.load_runtime_map(a.map) if a.map else None
    idx = sel.TestIndex(static, runtime, seconds)
    idx.map_source, idx.map_commit = a.map, getattr(a, "map_commit", None)
    return sel, rules, idx


def cmd_select(a):
    sel, rules, idx = _index(a)
    if a.changed:
        selected, full, reasons = sel.select(a.changed, {}, rules, idx)
        print("FULL suite" if full else "%d test files (%.1f%%)" % (len(selected), idx.share(selected)), dict(reasons))
        return 0
    with open(a.replay) as f:
        result = sel.replay(json.load(f)["prs"], rules, idx)
    if a.out:
        with open(a.out, "w") as f:
            json.dump(result, f, indent=1)
    print(json.dumps(result["summary"], indent=2))
    return 0


def cmd_affected(a):
    sel, rules, idx = _index(a)
    kinds = {f: "body-only" for f in a.body_only or ()}
    files = list(a.changed) + [f for f in a.body_only or () if f not in a.changed]
    if a.explain:
        if not a.out:
            raise ValueError("--explain needs --out decisions.json")
        r = sel.explain(files, kinds, rules, idx, map_source=idx.map_source, map_commit=idx.map_commit)
        with open(a.out, "w") as f:  # compact: the C encoder (indent forces the slow pure-Python one)
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
        print("decisions: %s -> %s" % (r["counts"], a.out), file=sys.stderr)
        print("\n".join(r["selected"]))
        return 0
    selected, full, _ = sel.select(files, kinds, rules, idx)
    print("\n".join(idx.test_files if full else sorted(selected)))
    return 0


def cmd_build_index(a):
    from engine import index  # noqa: PLC0415
    prof_path = _resolve_profile(a.profile)
    h = index.build(prof_path, a.static, a.map, a.out, map_commit=a.map_commit, map_date=a.map_date)
    m = h["meta"]
    rt_str = "%d runtime keys, " % m["runtime_keys"] if m.get("runtime_keys") is not None else ""
    map_parts = [f"{k} {m[f]}" for k, f in (("map", "map_source"), ("commit", "map_commit"), ("run", "map_run_id"), ("date", "map_date")) if m.get(f) is not None]
    map_tail = ("; " + " ".join(map_parts)) if map_parts else ""
    print("index %s: %d test files (+%d runtime-only), %d importer keys, %s%d bytes (static map %d bytes)%s"
          % (a.out, m["test_files"], m["runtime_only_test_files"], m["importer_keys"], rt_str, os.path.getsize(a.out),
             m["static_bytes"], map_tail))
    return 0


def _selection_args(p):
    p.add_argument("--profile", required=True)
    p.add_argument("--static", help="static import map {test_file: [repo files]} (engine/static_imports.py)")
    p.add_argument("--map", help="runtime map from the traced run (map_testfiles.json.gz)")
    p.add_argument("--map-commit", help="commit the runtime map was traced at (recorded in decisions)")
    p.add_argument("--index", help="compiled index from `engine.cli index` (replaces --static/--map; identical results)")


def cmd_verify(a):
    from engine import safety  # noqa: PLC0415
    r = safety.run(profile.load(_resolve_profile(a.profile)), a.junit, a.mutation, a.static, a.runtime, a.isolate)
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w") as f:
            json.dump(r, f, indent=1)
            f.write("\n")
    print(json.dumps(r["summary"], indent=2))
    for i, p in sorted(r["mutants"].items(), key=lambda kv: int(kv[0])):
        print("%3s %-6s %-10s killed=%-3d sel=%5.1f%% missed=%d %s" % (i, p["kind"], p["status"], len(p["killed_test_files"]),
                                                                       p["selected_pct"], len(p["missed"]), p["file"]))
    return 0


def cmd_flaky(a):
    from engine import flaky  # noqa: PLC0415
    audit = _audit()
    if a.provider != "github":
        print("flaky: provider %r not implemented yet (github only)" % a.provider, file=sys.stderr)
        return 2
    r = flaky.run_github(a.project, Window(a.days, a.end), audit.cache_dir(a.provider, a.project), workflows=a.workflow,
                         test_job_regex=a.test_job_regex, parsers=tuple(a.parser or ("pytest", "vitest")), events=a.event)
    out = a.out or os.path.join(audit.AUDITS, "flaky__%s__%s.json" % (a.provider, audit.common.slug(a.project)))
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w") as f:
        json.dump(r, f, indent=1)
        f.write("\n")
    c, t = r["cost_estimate"], r["test_job_logs_parsed"]
    print("%s %s: %d PR runs, %d flaky runs (%s%%) + %d new-run episodes, test logs parsed %d/%d, %d distinct flaky tests, "
          "~%d extra job-min, ~%d wait-min (estimate), categories %s -> %s"
          % (r["provider"], r["project"], r["pr_runs"], r["flaky_runs"], r["flaky_run_pct_of_pr_runs"], r["flaky_new_run"],
             t["ok"], t["total"], r["distinct_flaky_tests"], c["extra_job_minutes"], c["extra_wait_minutes"], r["categories"], out))
    return 0


def cmd_shadow(a):
    from engine import shadow  # noqa: PLC0415
    audit = _audit()
    if a.provider not in shadow.SOURCES:
        print("shadow: provider %r has no record source yet (%s)" % (a.provider, ", ".join(shadow.SOURCES)), file=sys.stderr)
        return 2
    sel, rules, idx = _index(a)
    records, changed_fn = shadow.SOURCES[a.provider](
        a.project, Window(a.days, a.end), audit.cache_dir(a.provider, a.project), workflows=a.workflow, events=a.event,
        test_job_regex=a.test_job_regex, parsers=tuple(a.parser or ("pytest",)))
    meta = {"provider": a.provider, "project": a.project, "window": Window(a.days, a.end).as_dict(), "workflows": a.workflow,
            "profile": a.profile, "index": a.index or {"static": a.static, "map": a.map}, "map_commit": idx.map_commit}
    r = shadow.run(records, shadow.select_fn_for(sel, rules, idx), changed_fn, set(idx.test_files), meta)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(r, f, indent=1)
        f.write("\n")
    s = r["summary"]
    print("shadow %s: %d runs, %d failing (%d with test ids, %d setup-only, %d flaky); failing tests caught %d, missed %d, "
          "flaky %d; recall %s%% -> %s" % (a.project, s["runs"], s["failing_runs"], s["failing_runs_with_test_ids"],
                                          s["setup_only_runs"], s["flaky_runs"], s["failing_tests_caught"],
                                          s["failing_tests_missed"], s["failing_tests_flaky"], s["recall_pct"], a.out))
    return 0


def cmd_report(a):
    from engine import report  # noqa: PLC0415
    m = report.run(a.audit, a.flaky, a.out, selection_path=a.selection, safety_path=a.safety, rates_path=a.rates)
    sc = m["scen"]
    print("report: %s (+ .html); per week today %.0f test job-min, %.1f h waiting%s"
          % (a.out, sc["today"]["test_min"], sc["today"]["wait_h"],
             "".join("; %s saves %.0f job-min, %.1f h, $%.0f" % (n, sc[n]["saved_min"], sc[n]["saved_wait_h"], sc[n]["saved_usd"])
                     for n in ("config", "selection") if n in sc)))
    return 0


def cmd_mcp(a):
    from engine import mcp_server  # noqa: PLC0415
    mcp_server.run_stdio_server()
    return 0


def cmd_not_yet(a):
    if getattr(a, "profile", None):
        profile.load(a.profile)  # fail early on a bad profile even before the command exists
    print("%s: not implemented yet (%s)" % (a.cmd, NOT_YET[a.cmd]), file=sys.stderr)
    return 2


def parser():
    ap = argparse.ArgumentParser(prog="ci-proof", description=__doc__.split("\n")[0])
    ap.add_argument("--version", action="version", version="ci-proof " + __version__)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="{audit,trace,select,verify,affected-tests,index,flaky,report,shadow,mcp}")

    p = sub.add_parser("audit", help="CI history audit for one project (read-only, cached)")
    p.add_argument("--provider", required=True, choices=_Providers(), metavar="PROVIDER")
    p.add_argument("--project", required=True, help="provider project id, e.g. owner/repo")
    p.add_argument("--days", type=int, required=True, help="number of full days before --end")
    p.add_argument("--end", required=True, help="exclusive end date YYYY-MM-DD (fixed, so re-runs are identical)")
    p.add_argument("--test-job-regex", help="jobs whose name matches are test jobs")
    p.add_argument("--full-run-rule", help="a run is full-suite if one of its job names matches")
    p.add_argument("--workflow", action="append", help="limit to this workflow file (GitHub, repeatable), e.g. ci.yaml")
    p.add_argument("--job", help="Prow job name (provider prow), e.g. pull-kubernetes-unit")
    p.add_argument("--out", help="summary path (default experiments/_audits/<provider>__<project>.json)")
    p.set_defaults(fn=cmd_audit)

    p = sub.add_parser("trace", help="traced run -> file-to-test map")
    p.add_argument("--profile", required=True)
    p.set_defaults(fn=cmd_not_yet)

    p = sub.add_parser("select", help="tests to run for a change, or replay recorded changes -> saving estimate")
    _selection_args(p)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--changed", nargs="+", metavar="FILE", help="changed files (treated as module-level edits)")
    g.add_argument("--replay", help='{"prs": [{pr, minutes, files, change_kinds}]}')
    p.add_argument("--seconds", help="real seconds per test file: weight the share by time instead of file count")
    p.add_argument("--out", help="write the full replay result (summary + per-PR rows)")
    p.set_defaults(fn=cmd_select)

    p = sub.add_parser("verify", help="safety check: planted bugs + isolated re-runs")
    p.add_argument("--profile", required=True)
    p.add_argument("--junit", required=True, help="dir with junit-b<batch>-g<group>.xml (batch 0 = baseline)")
    p.add_argument("--mutation", required=True, help="dir with mutants.json + batches.json")
    p.add_argument("--static", required=True, help="static import map {test_file: [repo files]}")
    p.add_argument("--runtime", required=True, help="runtime map from the traced run (map_testfiles.json.gz)")
    p.add_argument("--isolate", help="dir with junit-m<id>-r<k>.xml isolated re-runs (id 0 = no mutant)")
    p.add_argument("--out", help="write the full result (summary + per-mutant rows)")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("flaky", help="flaky CI runs: fail-then-pass per (workflow, commit), failing tests, cost (read-only, cached)")
    p.add_argument("--provider", required=True, choices=_Providers(), metavar="PROVIDER")
    p.add_argument("--project", required=True, help="provider project id, e.g. owner/repo")
    p.add_argument("--days", type=int, required=True, help="number of full days before --end")
    p.add_argument("--end", required=True, help="exclusive end date YYYY-MM-DD")
    p.add_argument("--workflow", action="append", help="limit to this workflow file (repeatable), e.g. ci.yaml")
    p.add_argument("--event", action="append", help="only runs of this trigger event (repeatable; default all PR events)")
    p.add_argument("--test-job-regex", default=r"(?i)test", help="jobs counted as test jobs for the log-parse rate")
    p.add_argument("--parser", action="append", choices=["pytest", "vitest", "jest"], help="log parsers (default pytest + vitest)")
    p.add_argument("--out", help="report path (default experiments/_audits/flaky__<provider>__<project>.json)")
    p.set_defaults(fn=cmd_flaky)

    p = sub.add_parser("report", help="customer audit report (markdown + self-contained html) from audit/flaky/selection JSON")
    p.add_argument("--audit", required=True, help="engine.cli audit summary JSON")
    p.add_argument("--flaky", required=True, help="engine.cli flaky report JSON")
    p.add_argument("--selection", help="engine.cli select --replay result JSON (enables the config-fix and selection columns)")
    p.add_argument("--safety", help="engine.cli verify result JSON (safety note)")
    p.add_argument("--rates", help="YAML/JSON overriding the default rates (runner $/min per OS, developer $/h, ...)")
    p.add_argument("--out", required=True, help="markdown path; the html goes next to it")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("shadow", help="passive shadow: real CI failures vs our selection (caught / missed / flaky / setup-only)")
    _selection_args(p)
    p.add_argument("--provider", required=True, metavar="PROVIDER", help="record source (engine/shadow.py SOURCES), e.g. github")
    p.add_argument("--project", required=True, help="provider project id, e.g. owner/repo")
    p.add_argument("--days", type=int, required=True, help="number of full days before --end")
    p.add_argument("--end", required=True, help="exclusive end date YYYY-MM-DD")
    p.add_argument("--workflow", action="append", help="limit to this workflow file (repeatable)")
    p.add_argument("--event", action="append", help="only runs of this trigger event (default all PR events)")
    p.add_argument("--test-job-regex", default=r"(?i)test", help="failed jobs whose logs are parsed for test ids")
    p.add_argument("--parser", action="append", choices=["pytest", "vitest", "jest"], help="log parsers (default pytest)")
    p.add_argument("--out", required=True, help="shadow.json path")
    p.set_defaults(fn=cmd_shadow)

    p = sub.add_parser("affected-tests", help="list affected test files for changed files")
    _selection_args(p)
    p.add_argument("--changed", nargs="+", required=True, metavar="FILE", help="changed files (module-level edits)")
    p.add_argument("--body-only", nargs="+", metavar="FILE", help="changed files whose edits are function-body only")
    p.add_argument("--explain", action="store_true",
                   help="also write typed decisions per test file (MUST_RUN|SAFE_TO_SKIP|UNCERTAIN + proof) to --out")
    p.add_argument("--out", help="decisions JSON path (with --explain)")
    p.set_defaults(fn=cmd_affected)

    p = sub.add_parser("index", help="compile the static + runtime maps into one fast index (once per map build)")
    p.add_argument("--profile", required=True)
    p.add_argument("--static", required=True, help="static import map {test_file: [repo files]}")
    p.add_argument("--map", help="runtime map from the traced run (map_testfiles.json.gz)")
    p.add_argument("--map-commit", help="commit the runtime map was traced at")
    p.add_argument("--map-date", help="when the map was traced (default: the map file's mtime)")
    p.add_argument("--out", required=True, help="index path, e.g. repo.idx")
    p.set_defaults(fn=cmd_build_index)

    p = sub.add_parser("mcp", help="run Model Context Protocol (MCP) server over stdio for Claude/Cursor")
    p.set_defaults(fn=cmd_mcp)
    return ap


def main(argv=None):
    a = parser().parse_args(argv)
    try:
        return a.fn(a)
    except Exception as e:  # noqa: BLE001
        common = sys.modules.get("engine.audit.common")
        if not isinstance(e, ValueError) and not (common and isinstance(e, common.HTTPStatusError)):
            raise
        print("error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
