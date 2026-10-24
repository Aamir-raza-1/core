"""Audit report v1 (AF2): waste + flaky + $ translation, rendered from engine outputs only.

  python3 -m engine.cli report --audit A.json --flaky F.json [--selection S.json] [--safety R.json] \
      [--rates rates.yaml] --out report.md          # also writes report.html next to it

Inputs (all engine outputs, nothing hand-typed):
  audit      engine.cli audit summary (engine/audit/common.py), with `extra` from common.breakdown
             (run kinds, wall-clock, every-run jobs, last-finishing job)
  flaky      engine.cli flaky report (engine/flaky.py)
  selection  engine.cli select --replay result (optional): summary + per-change rows with a change `label`
  safety     engine.cli verify result (optional)
  rates      YAML/JSON overriding DEFAULT_RATES; every default is shown in the report as an assumption

Everything is comparative: today (without) vs with config fixes vs with our selection, per week.
Every number carries a label (m measured, x extrapolated, e estimate, a assumption) and a footnote
with its formula and inputs. Model: `model(...)` (pure, golden-tested by contract V26); rendering:
`markdown(...)` / `html(...)` from one block list, so both formats always show the same numbers.
"""
import datetime as dt
import html as _html
import json
import os

from engine import profile

DEFAULT_RATES = {
    "runner_os": "linux",
    # GitHub-hosted standard runner list prices per minute (2-core Linux/Windows, 3-core macOS); check your plan
    "runner_usd_per_min": {"linux": 0.006, "windows": 0.010, "macos": 0.062},
    "public_repo": False,             # public repos on GitHub-hosted runners pay $0 for minutes
    "developer_usd_per_hour": 40,     # conservative global loaded rate; US is ~$75-100/h
    "productivity_loss": 0.4,         # share of waiting time that is really lost (cicdcost range 30-60%)
    "dev_rate_sensitivity": [25, 40, 75, 100],
    "map_rebuilds_per_week": 7,       # nightly traced full-suite run (map-freshness.md)
    "trace_overhead": 0.33,           # per-test coverage overhead on the traced run (1 pilot)
    "fast_selection_pct": 10,         # selections under this share of test files are assumed to finish like a partial run
    "weeks_per_year": 52,
}
RATE_NOTES = {
    "runner_os": "which runner price applies",
    "runner_usd_per_min": "runner list price, $/min per OS",
    "public_repo": "true = minutes are free (billed compute $0); list price shown for reference",
    "developer_usd_per_hour": "loaded developer cost, $/hour",
    "productivity_loss": "share of CI waiting that is really lost work",
    "dev_rate_sensitivity": "developer rates shown in the sensitivity table",
    "map_rebuilds_per_week": "traced full-suite runs per week that keep the map fresh",
    "trace_overhead": "extra run time of a traced run",
    "fast_selection_pct": "a selection below this % of test files is assumed to wait like a partial run",
    "weeks_per_year": "for yearly figures",
}
# change labels whose saving needs only a rule edit (no map); value = the generic fix
CONFIG_FIXABLE = {
    "tooling-only": "Changes that touch only tooling or lint configuration: run that tool's own checks, not the product test suite "
                    "(scope the tool's paths to its own test folder in the CI rules).",
    "deps-only": "Dependency-only changes (manifests, lockfiles, constraints): run the tests of the packages whose dependency changed, "
                 "not everything (scope dependency files to their consumers).",
}
LABELS = {"m": "measured", "x": "extrapolated", "e": "estimate", "a": "assumption"}


# ------------------------------------------------------------------------------------------- model

def load_rates(path=None):
    r = json.loads(json.dumps(DEFAULT_RATES))
    given = profile.read(path) if path else {}
    unknown = sorted(set(given) - set(DEFAULT_RATES))
    if unknown:
        raise ValueError("unknown rate keys: %s (known: %s)" % (", ".join(unknown), ", ".join(DEFAULT_RATES)))
    for k, v in given.items():
        if isinstance(v, dict) and isinstance(r[k], dict):
            r[k].update(v)
        else:
            r[k] = v
    if r["runner_os"] not in r["runner_usd_per_min"]:
        raise ValueError("runner_os %r has no price in runner_usd_per_min" % r["runner_os"])
    return r, sorted(given)


def _kinds(audit):
    k = (audit.get("extra") or {}).get("run_kinds") or {}
    if not k:
        raise ValueError("audit summary has no extra.run_kinds: re-run `engine.cli audit` (it adds wall-clock per run kind)")
    full = k.get("full") or {"runs": 0, "wall_min": {"median": None, "total": 0.0}, "test_job_min": 0.0, "job_min": 0.0}
    part = k.get("partial") or k.get("all")
    return full, part, k


def model(audit, flaky, selection=None, safety=None, rates=None):
    """All report numbers (plain values) from the engine outputs; no formatting."""
    rates = rates or load_rates()[0]
    full, part, kinds = _kinds(audit)
    days = audit["window"]["days"]
    wk = 7.0 / days
    fdays = flaky["window"]["days"]
    price = rates["runner_usd_per_min"][rates["runner_os"]]
    billed = 0.0 if rates["public_repo"] else price
    # waiting basis: runs × median wall-clock per run kind. Not the raw sum: a re-run moves a run's finish time by hours
    # (created_at stays, updated_at moves), which would count human delay as CI waiting.
    wall_total = sum(v["runs"] * (v["wall_min"]["median"] or 0.0) for v in kinds.values())
    test_min = audit["test_job_minutes"] or 0.0
    full_test = full.get("test_job_min") or 0.0
    m = {
        "days": days, "wk": wk, "price": price, "billed_price": billed,
        "runs": audit["runs"], "full_runs": full["runs"], "partial_runs": part["runs"] if part else 0,
        "full_run_share": audit["full_run_share"], "full_compute_share": audit["full_run_compute_share"],
        "test_min": test_min, "job_min": audit["job_minutes_total"], "full_test_min": full_test,
        "full_wall_median": full["wall_min"]["median"], "partial_wall_median": part["wall_min"]["median"] if part else None,
        "full_wall_p90": full["wall_min"].get("p90"), "partial_wall_p90": part["wall_min"].get("p90") if part else None,
        "full_exec_median": (full.get("exec_min") or {}).get("median"),
        "full_longest_job_median": (full.get("longest_job_min") or {}).get("median"),
        "wall_total": wall_total,
        "flaky_runs": flaky["flaky_runs"] + flaky.get("flaky_new_run", 0), "flaky_pct": flaky["flaky_run_pct_of_pr_runs"],
        "flaky_pr_runs": flaky["pr_runs"], "flaky_days": fdays,
        "flaky_job_min_wk": flaky["cost_estimate"]["extra_job_minutes"] * 7.0 / fdays,
        "flaky_wait_h_wk": flaky["cost_estimate"]["extra_wait_minutes"] / 60.0 * 7.0 / fdays,
    }
    today = {"full_runs": full["runs"] * wk, "test_min": test_min * wk, "job_min": m["job_min"] * wk, "wait_h": wall_total / 60.0 * wk}
    scen = {"today": dict(today)}
    if selection:
        rows, s = selection["prs"], selection["summary"]
        tot = sum(r["minutes"] for r in rows) or 1.0
        n = len(rows) or 1
        cfg = [r for r in rows if r.get("label") in CONFIG_FIXABLE]
        fast = rates["fast_selection_pct"]
        m.update({
            "sel_prs": len(rows), "sel_total_pct": s["estimated_saving_pct"],
            "cfg_pct": round(100.0 * sum(r["minutes"] * (1 - r["pct_test_files"] / 100.0) for r in cfg) / tot, 1),
            "cfg_prs": len(cfg),
            "cfg_nonfull_share": sum(r["pct_test_files"] < 100 for r in cfg) / n,
            "cfg_fast_share": sum(r["pct_test_files"] < fast for r in cfg) / n,
            "sel_nonfull_share": sum(r["pct_test_files"] < 100 for r in rows) / n,
            "sel_fast_share": sum(r["pct_test_files"] < fast for r in rows) / n,
        })
        m["tool_pct"] = round(m["sel_total_pct"] - m["cfg_pct"], 1)
        per_full = full_test / full["runs"] if full["runs"] else 0.0
        m["map_min_wk"] = rates["map_rebuilds_per_week"] * per_full * (1 + rates["trace_overhead"])
        dwall = max(0.0, (m["full_wall_median"] or 0) - (m["partial_wall_median"] or 0))
        for name, pct, nonfull, fastshare, upkeep in (("config", m["cfg_pct"], m["cfg_nonfull_share"], m["cfg_fast_share"], 0.0),
                                                      ("selection", m["sel_total_pct"], m["sel_nonfull_share"], m["sel_fast_share"], m["map_min_wk"])):
            saved = full_test * wk * pct / 100.0
            wsaved = full["runs"] * wk * fastshare * dwall / 60.0
            scen[name] = {"full_runs": today["full_runs"] * (1 - nonfull), "test_min": today["test_min"] - saved + upkeep,
                          "job_min": today["job_min"] - saved + upkeep, "wait_h": today["wait_h"] - wsaved,
                          "saved_min": saved - upkeep, "saved_wait_h": wsaved, "upkeep_min": upkeep}
    for s in scen.values():
        s["compute_usd"] = s["job_min"] * billed
        s["compute_list_usd"] = s["job_min"] * price
        s["wait_usd"] = s["wait_h"] * rates["productivity_loss"] * rates["developer_usd_per_hour"]
    for name in ("config", "selection"):
        if name in scen:
            s = scen[name]
            s["saved_usd"] = s["saved_min"] * billed + s["saved_wait_h"] * rates["productivity_loss"] * rates["developer_usd_per_hour"]
    m["flaky_usd_wk"] = m["flaky_job_min_wk"] * billed + m["flaky_wait_h_wk"] * rates["productivity_loss"] * rates["developer_usd_per_hour"]
    m["scen"] = scen
    if safety:
        m["safety"] = safety["summary"]
    return m


# ------------------------------------------------------------------------------------- formatting

class N:
    """A labelled number: value + kind (m/x/e/a) + footnote formula."""

    def __init__(self, value, kind, note, fmt="{:,.0f}", pre="", post=""):
        self.value, self.kind, self.note, self.fmt, self.pre, self.post = value, kind, note, fmt, pre, post

    def text(self):
        if self.value is None:
            return "n/a"
        return self.pre + self.fmt.format(self.value) + self.post


class Doc:
    def __init__(self):
        self.blocks, self.notes = [], []

    def fn(self, n):
        key = (n.kind, n.note)
        if key not in self.notes:
            self.notes.append(key)
        return self.notes.index(key) + 1

    def add(self, kind, *a):
        self.blocks.append((kind,) + a)


def _usd(v, kind, note):
    return N(v, kind, note, "{:,.0f}", "$")


def _h(v, kind, note):
    return N(v, kind, note, "{:,.1f}", post=" h")


def build(audit, flaky, selection, safety, rates, given):
    m = model(audit, flaky, selection, safety, rates)
    d = Doc()
    P, W, days, wk = audit["project"], audit["window"], m["days"], m["wk"]
    sc = m["scen"]
    has_sel = "selection" in sc
    params = (audit.get("extra") or {}).get("params") or {}
    wfs = params.get("workflows") or []
    wf = wfs[0] if wfs else None
    last_day = (dt.date.fromisoformat(W["end_exclusive"]) - dt.timedelta(days=1)).isoformat()
    win = "%s to %s (%d days)" % (W["start"], last_day, days)
    dev, loss = rates["developer_usd_per_hour"], rates["productivity_loss"]
    price_note = "runner list price %s $%s/min (%s)%s" % (rates["runner_os"], rates["runner_usd_per_min"][rates["runner_os"]],
                                                         "from rates file" if "runner_usd_per_min" in given else "default, editable",
                                                         "; public repo: billed $0" if rates["public_repo"] else "")
    wait_note = "waiting $ = waiting hours × productivity loss %s × developer $%s/h (%s)" % (
        loss, dev, "from rates file" if "developer_usd_per_hour" in given else "default assumption, editable")
    x_week = "× 7/%d to a week" % days

    def X(v, kind, note, **kw):
        return N(v, kind, note, **kw)

    # ---- title + summary
    d.add("h", 1, "CI Rules Audit: %s" % P)
    d.add("p", ["Read-only audit of pull-request CI (%s, %s). Generated by the engine (`engine.cli report`) from the audit, flaky%s outputs; "
                "no number is typed by hand. Labels: _m_ measured, _x_ extrapolated, _e_ estimate, _a_ assumption (editable); "
                "every number has a footnote with its formula." % (", ".join(wfs) or "all workflows", win,
                                                                   ", selection" if has_sel else "")])
    d.add("h", 2, "1. Summary")
    d.add("p", ["**What developers wait for is the wall-clock of a run, not its compute.** A full-suite run takes ",
                X(m["full_wall_median"], "m", "median created→finished of full-suite PR runs (queue included), audit extra.run_kinds.full.wall_min", fmt="{:,.1f}", post=" min"),
                " (median) vs ", X(m["partial_wall_median"], "m", "median created→finished of partial PR runs, audit extra.run_kinds.partial.wall_min", fmt="{:,.1f}", post=" min"),
                " for a partial run. Tests run in parallel jobs, so cutting compute does not cut waiting one-for-one: below, waiting is only counted "
                "as saved for runs whose selection drops under %s%% of test files (they are assumed to finish like a partial run)." % rates["fast_selection_pct"]])
    hdr = ["Per week", "Today (without)", "With config fixes", "With our selection"]

    def row(label, key, mk):
        cells = [label]
        for name in ("today", "config", "selection"):
            cells.append(mk(name, sc[name][key]) if name in sc else "needs Proof Pack (selection replay)")
        return cells
    tnote = "today: audit test_job_minutes %s %s; config/selection: − full-run test minutes %s × 7/%d × saving %% (config %s%%, selection %s%%, replay of %s full-suite changes, extrapolated to all full runs)%s" % (
        "{:,.0f}".format(m["test_min"]), x_week, "{:,.0f}".format(m["full_test_min"]), days, m.get("cfg_pct"), m.get("sel_total_pct"),
        m.get("sel_prs", "n/a"), "; selection adds map upkeep %s job-min" % "{:,.0f}".format(m["map_min_wk"]) if has_sel else "")
    wnote = ("today: Σ over run kinds of runs × median created→finished (full %s × %s min + partial %s × %s min) / 60 %s; "
             "config/selection: − full runs/week × share of replayed changes selected under %s%% (config %s, selection %s) × (%s − %s min) / 60" % (
                 m["full_runs"], m["full_wall_median"], m["partial_runs"], m["partial_wall_median"], x_week, rates["fast_selection_pct"],
                 round(m.get("cfg_fast_share", 0), 3), round(m.get("sel_fast_share", 0), 3), m["full_wall_median"], m["partial_wall_median"]))
    rows = [
        row("Full-suite PR runs", "full_runs", lambda n, v: X(v, "x" if n == "today" else "e",
            "today: audit full runs %s %s; config/selection: × (1 − share of replayed changes that no longer need the full suite: config %s, selection %s)" % (
                m["full_runs"], x_week, round(m.get("cfg_nonfull_share", 0), 3), round(m.get("sel_nonfull_share", 0), 3)))),
        row("Test compute (job-min)", "test_min", lambda n, v: X(v, "x" if n == "today" else "e", tnote)),
        row("Compute $ billed", "compute_usd", lambda n, v: _usd(v, "e", "all PR job-minutes per week × " + price_note)),
        row("Developer wait on CI (hours)", "wait_h", lambda n, v: _h(v, "e", wnote)),
        row("Waiting $ (lost work)", "wait_usd", lambda n, v: _usd(v, "e", wait_note)),
    ]
    d.add("table", hdr, rows)
    d.add("p", ["Flaky CI on top, unchanged by either column (fix: see §3): ",
                X(m["flaky_job_min_wk"], "e", "flaky cost_estimate.extra_job_minutes (job-minutes of failed attempts in flaky episodes) × 7/%d" % m["flaky_days"]),
                " job-min and ", _h(m["flaky_wait_h_wk"], "e", "flaky cost_estimate.extra_wait_minutes / 60 × 7/%d" % m["flaky_days"]),
                " of waiting per week (", _usd(m["flaky_usd_wk"], "e", "flaky job-min × billed rate + flaky wait h × %s × $%s/h" % (loss, dev)), ")."])
    # ---- cost of doing nothing
    d.add("h", 3, "What you lose by doing nothing")
    if has_sel:
        yr = rates["weeks_per_year"]
        lrows = []
        for name, label in (("config", "Config fixes (free: 1-3 rule edits, you can do them yourself)"), ("selection", "Our selection (includes the config fixes, net of map upkeep)")):
            s = sc[name]
            lrows.append([label,
                          X(s["saved_min"], "e", "test compute saved per week = full-run test minutes × saving % − map upkeep"),
                          _h(s["saved_wait_h"], "e", "waiting saved per week = full runs × share of changes selected under %s%% × (full − partial median wall-clock) / 60" % rates["fast_selection_pct"]),
                          _usd(s["saved_usd"], "e", "saved $ = saved minutes × billed rate + saved waiting h × %s × $%s/h" % (loss, dev)),
                          _usd(s["saved_usd"] * yr, "x", "saved $ per week × %s weeks" % yr)])
        lrows.append(["Flaky re-runs (not fixed by either; fix the causes in §3)",
                      X(m["flaky_job_min_wk"], "e", "flaky cost_estimate.extra_job_minutes (job-minutes of failed attempts in flaky episodes) × 7/%d" % m["flaky_days"]),
                      _h(m["flaky_wait_h_wk"], "e", "flaky cost_estimate.extra_wait_minutes / 60 × 7/%d" % m["flaky_days"]),
                      _usd(m["flaky_usd_wk"], "e", "flaky job-min × billed rate + flaky wait h × %s × $%s/h" % (loss, dev)),
                      _usd(m["flaky_usd_wk"] * yr, "x", "flaky $ per week × %s weeks" % yr)])
        d.add("table", ["Left on the table", "Compute (job-min/week)", "Developer waiting (h/week)", "$ / week", "$ / year"], lrows)
    else:
        d.add("p", ["Savings need the selection replay (Proof Pack). Today's costs above are what the audit measured."])
    d.add("box", "Verify this yourself", [
        "The headline inputs are the audit window's PR runs. Count them:",
        "gh run list -R %s %s--event pull_request --created %s..%s --status completed --limit 2000 --json databaseId,conclusion,createdAt,updatedAt > runs.json"
        % (P, ("--workflow %s " % wf) if wf else "", W["start"], last_day),
        "jq '[.[] | select(.conclusion==\"success\" or .conclusion==\"failure\")] | length' runs.json   # pull_request runs; the audit also counts pull_request_target",
        "Wall-clock per run = updatedAt − createdAt (what the developer sees as the run's total time on the PR).",
    ])

    # ---- waste
    d.add("h", 2, "2. Waste: where the full suite runs")
    d.add("table", ["Measure", "Value"], [
        ["Completed PR runs", X(m["runs"], "m", "audit runs: completed PR runs (success + failure), %s" % win)],
        ["Runs that ran the full suite", X(m["full_run_share"], "m", "audit full_run_share = runs with a job matching %r ÷ runs" % params.get("full_run_rule"), fmt="{:.1f}", post="%")],
        ["Share of test compute in full runs", X(m["full_compute_share"], "m", "audit full_run_compute_share = test-job minutes in full runs ÷ all test-job minutes (test job = %r)" % params.get("test_job_regex"), fmt="{:.1f}", post="%")],
        ["Test-job minutes in the window", X(m["test_min"], "m", "audit test_job_minutes")],
        ["All job minutes in the window", X(m["job_min"], "m", "audit job_minutes_total")],
    ])
    if has_sel:
        d.add("p", ["Replaying ", X(m["sel_prs"], "m", "selection summary.prs: recorded full-suite changes replayed through the engine's selection"),
                    " real full-suite changes: our selection would save ", X(m["sel_total_pct"], "e", "selection summary.estimated_saving_pct = 1 − Σ minutes × selected share ÷ Σ minutes; " + selection["summary"].get("note", ""), fmt="{:.1f}", post="%"),
                    " of full-run test compute; ", X(m["cfg_pct"], "e", "Σ over changes labelled %s of minutes × (1 − selected share) ÷ Σ minutes" % "/".join(CONFIG_FIXABLE), fmt="{:.1f}", post="%"),
                    " of it is **config-fixable** (rule edits, no tool) and ", X(m["tool_pct"], "e", "total saving % − config-fixable %", fmt="{:.1f}", post="%"),
                    " needs the file→test map."])
        crow, ex = [], {}
        for r in selection["prs"]:
            if r.get("label") in CONFIG_FIXABLE:
                ex.setdefault(r["label"], []).append(r)
        for lab, fix in CONFIG_FIXABLE.items():
            rs = ex.get(lab, [])
            if not rs:
                continue
            tot = sum(r["minutes"] for r in selection["prs"])
            crow.append(["%s (%d change%s)" % (lab, len(rs), "" if len(rs) == 1 else "s"), fix,
                         X(100.0 * sum(r["minutes"] * (1 - r["pct_test_files"] / 100.0) for r in rs) / tot, "e",
                           "Σ over %s changes of minutes × (1 − selected share) ÷ Σ replayed minutes" % lab, fmt="{:.1f}", post="%"),
                         ", ".join("#%s" % r["pr"] for r in rs[:4])])
        d.add("table", ["Rule to fix", "Concrete fix", "Share of full-run compute", "Example PRs"], crow)
        still = {}
        for r in selection["prs"]:
            if r["pct_test_files"] == 100.0:
                for k in r.get("reasons", {}):
                    if k.startswith("full: "):
                        still[k[6:]] = still.get(k[6:], 0) + 1
        if still:
            d.add("p", ["Files that still force the full suite even with our selection (by design, until scoped): " +
                        ", ".join("`%s` (%d)" % kv for kv in sorted(still.items(), key=lambda kv: (-kv[1], kv[0]))[:6]) + "."])
    every = (audit.get("extra") or {}).get("every_run_jobs") or []
    if every:
        thr = audit["extra"].get("every_run_pct_threshold", 90)
        d.add("p", ["Jobs that run on (almost) every PR regardless of the change (in ≥%s%% of runs), by minutes:" % thr])
        d.add("table", ["Job", "Runs it ran in", "Job-min in window", "Median min"], [
            [j["job"], X(j["runs_pct"], "m", "audit extra.every_run_jobs: runs containing the job ÷ runs", fmt="{:.1f}", post="%"),
             X(j["job_min"], "m", "Σ job minutes (started→completed) of that job name"),
             X(j["median_min"], "m", "median job minutes", fmt="{:.1f}")] for j in every[:8]])
    vb = ["Pick a full run and a partial run from runs.json, then list their jobs:",
          "gh run view <run id> -R %s --json jobs --jq '.jobs[] | select(.conclusion != \"skipped\") | .name'" % P,
          "A run is full-suite if a job matches %r; test jobs match %r." % (params.get("full_run_rule"), params.get("test_job_regex"))]
    if has_sel and ex:
        r0 = next(iter(ex.values()))[0]
        vb += ["Check a config-fixable change (#%s): its files are only tooling or dependency files, yet the full suite ran." % r0["pr"],
               "gh pr view %s -R %s --json files --jq '.files[].path'" % (r0["pr"], P),
               "gh pr checks %s -R %s" % (r0["pr"], P)]
    d.add("box", "Verify this yourself", vb)

    # ---- flaky
    d.add("h", 2, "3. Flaky CI")
    d.add("p", [X(m["flaky_runs"], "m", "flaky flaky_runs + flaky_new_run: a failed attempt then a passed attempt on the same (workflow, commit), no code change"),
                " of ", X(m["flaky_pr_runs"], "m", "flaky pr_runs"), " PR runs (", X(m["flaky_pct"], "m", "flaky flaky_run_pct_of_pr_runs", fmt="{:.2f}", post="%"),
                ") failed and then passed on the same commit. Cost per week: ",
                X(m["flaky_job_min_wk"], "e", "flaky cost_estimate.extra_job_minutes (job-minutes of failed attempts in flaky episodes) × 7/%d" % m["flaky_days"]), " job-min and ",
                _h(m["flaky_wait_h_wk"], "e", "flaky cost_estimate.extra_wait_minutes / 60 × 7/%d" % m["flaky_days"]), " of developer waiting."])
    cats = flaky.get("categories") or {}
    q = {"timeout": "Which step times out, and is its limit or its runner too tight?",
         "lint/typecheck": "Why does a lint/type check pass on re-run with no code change (cache, ordering, network fetch)?",
         "infra/upload": "Should an upload/reporting step be allowed to fail the run, or retry by itself?",
         "test": "Which tests share state or time, and can they be quarantined until fixed?",
         "unknown": "What failed? The log did not show a recognisable cause."}
    fix = {"timeout": "Lower test parallelism on the runner (or a bigger runner), raise the timeout only for the heavy tests, split the slowest suite into its own job.",
           "lint/typecheck": "Pin the tool and plugin versions, make its cache key include the lockfile, avoid network fetches during the check.",
           "infra/upload": "Retry only that step (or let reporting/upload steps not fail the run); keep test results as the gate.",
           "test": "Quarantine the named tests in a non-blocking job, reproduce them under CI settings (same parallelism, repeated), reset shared state per test file.",
           "unknown": "Add a test reporter (junit) so failures name the test; then re-run this report."}
    d.add("table", ["Category (from logs and job names)", "Episodes", "Question for the team", "Free fix to try first"],
          [[c, X(n, "m", "flaky categories: evidence from the failed job name and parsed log"), q.get(c, "What changed between the two attempts?"),
            fix.get(c, "Compare the failed and the passed attempt's logs; quarantine if it repeats.")]
           for c, n in sorted(cats.items(), key=lambda kv: -kv[1])])
    if flaky.get("top_files"):
        d.add("p", ["Flaky test files: " + ", ".join("`%s` (%d)" % (f, n) for f, n in flaky["top_files"][:6]) + "."])
    det = flaky.get("flaky_runs_detail") or []
    fb = ["List a flaky run's attempts and what failed first:"]
    if det:
        rid = det[0]["run_id"]
        fb += ["gh run view %s -R %s --attempt 1 --json jobs --jq '.jobs[] | select(.conclusion == \"failure\") | .name'" % (rid, P),
               "gh run view %s -R %s --json attempt,conclusion,headSha   # later attempt passed, same headSha" % (rid, P),
               "UI: https://github.com/%s/actions/runs/%s/attempts/1" % (P, rid)]
    d.add("box", "Verify this yourself", fb)

    # ---- critical path
    d.add("h", 2, "4. Critical path: what developers actually wait for")
    lj = ((audit.get("extra") or {}).get("last_job") or {}).get("full") or []
    d.add("table", ["", "Full-suite run", "Partial run"], [
        ["Wall-clock, created→finished (median)", X(m["full_wall_median"], "m", "median created→finished of full-suite PR runs (queue included), audit extra.run_kinds.full.wall_min", fmt="{:,.1f}", post=" min"),
         X(m["partial_wall_median"], "m", "median created→finished of partial PR runs, audit extra.run_kinds.partial.wall_min", fmt="{:,.1f}", post=" min")],
        ["Wall-clock p90", X(m["full_wall_p90"], "m", "p90 created→finished of full-suite PR runs", fmt="{:,.1f}", post=" min"),
         X(m["partial_wall_p90"], "m", "p90 created→finished of partial PR runs", fmt="{:,.1f}", post=" min")],
        ["Test compute per run (job-min, mean)", X(m["full_test_min"] / m["full_runs"] if m["full_runs"] else None, "m", "full-run test-job minutes ÷ full runs", fmt="{:,.1f}"),
         X(((audit["extra"]["run_kinds"].get("partial") or {}).get("test_job_min") or 0) / m["partial_runs"] if m["partial_runs"] else None, "m", "partial-run test-job minutes ÷ partial runs", fmt="{:,.1f}")],
    ])
    d.add("p", ["A full run spends far more compute than it takes wall-clock because its test jobs run in parallel; the slowest single job takes ",
                X(m["full_longest_job_median"], "m", "median over full runs of the longest job's started→completed", fmt="{:,.1f}", post=" min"),
                " (median). " + ("The job that most often finishes last: %s." % ", ".join("`%s` (%d runs)" % (n, c) for n, c in lj[:3]) if lj else "") +
                " **Compute saved ≠ waiting saved:** skipping tests in a parallel stage saves minutes but only shortens the wait once the remaining work no longer dominates the run."])
    cb = ["Open any full run in the UI (Actions → run → Summary) and read 'Total duration' and the job graph.",
          "gh run view <run id> -R %s --json createdAt,updatedAt,jobs --jq '{wall: [.createdAt, .updatedAt], last: (.jobs | max_by(.completedAt) | .name)}'" % P]
    d.add("box", "Verify this yourself", cb)

    # ---- cost in $
    d.add("h", 2, "5. Cost in $: compute vs waiting (inputs editable)")
    d.add("p", ["Two different dollars: **compute $** (runner minutes × price) and **waiting $** (developer hours lost while CI runs). "
                "Waiting is counted on the critical path above, never as compute minutes. " +
                ("This repository is public: GitHub-hosted minutes are free, so billed compute $ is $0; the list-price equivalent is shown for reference."
                 if rates["public_repo"] else "")])
    crows = [[lab] + [(_usd(sc[n][k], "e", note) if n in sc else "needs Proof Pack") for n in ("today", "config", "selection")]
             for lab, k, note in (("Compute $ billed / week", "compute_usd", "all PR job-minutes per week × " + price_note),
                                  ("Compute $ at list price / week", "compute_list_usd", "all PR job-minutes per week × list price $%s/min" % m["price"]),
                                  ("Waiting $ / week", "wait_usd", wait_note))]
    d.add("table", hdr, crows)
    if has_sel:
        srows = []
        for r in rates["dev_rate_sensitivity"]:
            srows.append(["$%s/h%s" % (r, " (default)" if r == dev else "")] + [
                _usd(sc[n]["saved_wait_h"] * loss * r + sc[n]["saved_min"] * m["billed_price"], "e",
                     "saved $ per week at a developer rate R: saved waiting h × %s × R + saved job-min × billed rate" % loss) for n in ("config", "selection")])
        d.add("p", ["How the weekly saving changes with the developer rate (everything else unchanged):"])
        d.add("table", ["Developer rate", "Config fixes saves / week", "Our selection saves / week"], srows)
        parts = ["Compute today at each runner list price (billed only for private repos or paid runners): "]
        for i, (o, pr) in enumerate(rates["runner_usd_per_min"].items()):
            parts += [("; " if i else "") + "%s ($%s/min): " % (o, pr),
                      _usd(sc["today"]["job_min"] * pr, "e", "all PR job-minutes per week × runner list price of that OS")]
        d.add("p", parts + [" per week."])
        d.add("p", ["Map upkeep is already subtracted from the selection column: ",
                    X(m["map_min_wk"], "e", "map rebuilds/week %s × mean full-run test minutes × (1 + trace overhead %s)" % (rates["map_rebuilds_per_week"], rates["trace_overhead"])),
                    " job-min per week."])
    d.add("p", ["Rates used (edit with `--rates rates.yaml`; keys below):"])
    d.add("table", ["Key", "Value", "Source", "Meaning"],
          [[k, json.dumps(rates[k]), "rates file" if k in given else "**default assumption**", RATE_NOTES[k]] for k in DEFAULT_RATES])
    d.add("box", "Verify this yourself", [
        "Runner prices: your GitHub plan's Actions pricing page; your real spend: Settings → Billing and licensing → Usage → Actions, filtered to this repository.",
        "Re-run with your own numbers: python3 -m engine.cli report --audit A.json --flaky F.json --selection S.json --rates my_rates.yaml --out report.md",
        "my_rates.yaml example: developer_usd_per_hour: 90 / productivity_loss: 0.3 / runner_os: linux / public_repo: false"])

    # ---- safety
    d.add("h", 2, "6. Safety note")
    if safety:
        s = safety["summary"]
        d.add("p", ["Selection is only worth it if it misses nothing. Planted-bug check on this repository: ",
                    X(s.get("caught"), "m", "verify summary.caught: planted bugs with ≥1 newly failing test file"), " of ",
                    X(s.get("mutants"), "m", "verify summary.mutants"), " bugs caught, ",
                    X(s.get("missed_test_files_total"), "m", "verify summary.missed_test_files_total: failing test files the selection did not pick"),
                    " missed test files, ", X(s.get("unresolved_failures"), "m", "verify summary.unresolved_failures (counted as unresolved, never as passes)"),
                    " unresolved. A small sample; the Proof Pack repeats it on your code plus 2 weeks of shadow mode."])
    else:
        d.add("p", ["No safety run yet. Before anything is skipped, the Proof Pack measures misses with planted bugs and a shadow mode."])
    d.add("p", ["Safety rails in any setup: the full suite still runs on main and before release; unknown files and core dependencies run everything; a label forces a full run."])
    d.add("box", "Verify this yourself", ["The safety result is reproducible from its artifacts: python3 -m engine.cli verify --profile <profile> --junit <dir> --mutation <dir> --static <map> --runtime <map> [--isolate <dir>]"])

    # ---- method
    d.add("h", 2, "7. Method and labels")
    d.add("ul", ["_m_ **measured**: counted from CI data in the window.",
                 "_x_ **extrapolated**: scaled from the window to a week or year (× 7/%d, × %s)." % (days, rates["weeks_per_year"]),
                 "_e_ **estimate**: uses an assumption (rates, the replay sample standing for all full runs, the wait model); the assumption is in the footnote.",
                 "_a_ **assumption**: an input you can edit (rates table above).",
                 "Audit: %s; %s." % ("; ".join(audit.get("sources") or []), audit.get("method", "")),
                 "Flaky: %s." % flaky.get("method", ""),
                 ("Selection: %s replayed changes. %s" % (m.get("sel_prs"), selection["summary"].get("note", ""))) if has_sel else "Selection: not run.",
                 "Windows: audit %s, {:,} completed PR runs; flaky %s to %s, {:,} PR runs%s.".format(audit["runs"], flaky["pr_runs"]) % (
                     win, flaky["window"]["start"],
                     (dt.date.fromisoformat(flaky["window"]["end_exclusive"]) - dt.timedelta(days=1)).isoformat(),
                     "" if flaky["pr_runs"] == audit["runs"] else " (the two reads differ by %d run(s): a run's latest attempt can change between reads)" % abs(flaky["pr_runs"] - audit["runs"])),
                 ])
    return d, m


# --------------------------------------------------------------------------------------- render

def _cell_md(c, d):
    if isinstance(c, N):
        return "%s _%s_[^%d]" % (c.text(), c.kind, d.fn(c)) if c.value is not None else "n/a"
    return str(c).replace("|", "\\|")


def _parts_md(parts, d):
    return "".join(_cell_md(p, d) if isinstance(p, N) else p for p in parts)


def markdown(d):
    out = []
    for b in d.blocks:
        k = b[0]
        if k == "h":
            out.append("#" * b[1] + " " + b[2])
        elif k == "p":
            out.append(_parts_md(b[1], d))
        elif k == "ul":
            out.append("\n".join("- " + x for x in b[1]))
        elif k == "table":
            out.append("| " + " | ".join(b[1]) + " |\n|" + "---|" * len(b[1]) + "\n" +
                       "\n".join("| " + " | ".join(_cell_md(c, d) for c in r) + " |" for r in b[2]))
        elif k == "box":
            out.append("> **%s**\n>\n" % b[1] + "\n".join("> `%s`  " % x if _is_cmd(x) else "> %s  " % x for x in b[2]))
    out.append("---\n\n" + "\n".join("[^%d]: **%s**: %s" % (i + 1, LABELS[k], n) for i, (k, n) in enumerate(d.notes)))
    return "\n\n".join(out) + "\n"


def _is_cmd(x):
    return x.startswith(("gh ", "jq ", "python3 "))


CSS = """
:root{--bg:#fff;--fg:#1d2330;--mut:#5b6474;--line:#dfe3ea;--box:#f4f7fb;--acc:#2456c9;--m:#1f7a4d;--x:#8a5a00;--e:#a33a3a;--a:#5a4bb0}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#14171d;--fg:#e6e9ef;--mut:#9aa3b2;--line:#2c323d;--box:#1b2029;--acc:#7aa2ff;--m:#5fd39a;--x:#e0b25c;--e:#ff8a8a;--a:#b2a6ff}}
:root[data-theme="dark"]{--bg:#14171d;--fg:#e6e9ef;--mut:#9aa3b2;--line:#2c323d;--box:#1b2029;--acc:#7aa2ff;--m:#5fd39a;--x:#e0b25c;--e:#ff8a8a;--a:#b2a6ff}
body{background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:24px 16px}
main{max-width:960px;margin:0 auto}h1{font-size:26px}h2{font-size:20px;margin-top:36px;border-bottom:1px solid var(--line);padding-bottom:4px}
table{border-collapse:collapse;width:100%;margin:12px 0;font-size:14px;display:block;overflow-x:auto}th,td{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{background:var(--box)}td:not(:first-child){white-space:nowrap}.box{background:var(--box);border-left:4px solid var(--acc);padding:10px 14px;margin:14px 0;border-radius:4px}
.box code{display:block;white-space:pre-wrap;word-break:break-all;font-size:12.5px;margin:4px 0}code{font-family:ui-monospace,Menlo,monospace}
sup a{text-decoration:none;font-size:11px;font-weight:600}sup.m a{color:var(--m)}sup.x a{color:var(--x)}sup.e a{color:var(--e)}sup.a a{color:var(--a)}
.notes{font-size:12.5px;color:var(--mut)}.notes li{margin:2px 0}
"""


def _inline(s):
    s = _html.escape(s)
    import re  # noqa: PLC0415
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    return re.sub(r"(?<![\w])_([mxea])_(?![\w])", r"<em>\1</em>", s)


def _cell_html(c, d):
    if isinstance(c, N):
        if c.value is None:
            return "n/a"
        i = d.fn(c)
        return '%s <sup class="%s"><a href="#fn%d" title="%s">%s%d</a></sup>' % (_html.escape(c.text()), c.kind, i, LABELS[c.kind], c.kind, i)
    return _inline(str(c))


def html(d, title):
    out = []
    for b in d.blocks:
        k = b[0]
        if k == "h":
            out.append("<h%d>%s</h%d>" % (b[1], _inline(b[2]), b[1]))
        elif k == "p":
            out.append("<p>%s</p>" % "".join(_cell_html(p, d) if isinstance(p, N) else _inline(p) for p in b[1]))
        elif k == "ul":
            out.append("<ul>%s</ul>" % "".join("<li>%s</li>" % _inline(x) for x in b[1]))
        elif k == "table":
            out.append("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (
                "".join("<th>%s</th>" % _inline(h) for h in b[1]),
                "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % _cell_html(c, d) for c in r) for r in b[2])))
        elif k == "box":
            out.append('<div class="box"><strong>%s</strong>%s</div>' % (_html.escape(b[1]), "".join(
                "<code>%s</code>" % _html.escape(x) if _is_cmd(x) else "<p>%s</p>" % _inline(x) for x in b[2])))
    out.append('<ol class="notes">%s</ol>' % "".join('<li id="fn%d"><strong>%s</strong>: %s</li>' % (i + 1, LABELS[k], _html.escape(n))
                                                      for i, (k, n) in enumerate(d.notes)))
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>%s</title><style>%s</style></head><body><main>\n%s\n</main></body></html>\n" % (_html.escape(title), CSS, "\n".join(out)))


def _load(path):
    if not path:
        return None
    with open(path) as f:
        return json.load(f)


def run(audit_path, flaky_path, out, selection_path=None, safety_path=None, rates_path=None):
    """Write <out> (markdown) and <out minus .md>.html; return the model."""
    rates, given = load_rates(rates_path)
    audit, flaky = _load(audit_path), _load(flaky_path)
    d, m = build(audit, flaky, _load(selection_path), _load(safety_path), rates, given)
    md = markdown(d)  # footnote order is fixed by the markdown pass; html reuses it
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w") as f:
        f.write(md)
    with open(os.path.splitext(out)[0] + ".html", "w") as f:
        f.write(html(d, "CI Rules Audit"))
    return m
