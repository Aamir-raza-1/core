"""Shared audit pieces: cached HTTP, the time window, and the common summary every provider emits.

Provider adapters (engine/audit/<provider>.py) implement
    fetch_runs(project, window, cache_dir, **opts) -> [normalized run]
where a normalized run is
    {id, workflow, created_at, started_at, finished_at, status: success|failure|cancelled, attempt,
     commit, ref_kind: pr|branch, jobs: [{name, started_at, finished_at, status}]}
(timestamps ISO 8601). `workflow` is the pipeline/workflow name (one commit runs several:
lint, tests, docs…). `started_at` may be None; the earliest job start is used instead.
An adapter may emit one record per attempt; `summarize` treats each record as one run.
The summary keys are fixed here; adapters may only add an `extra` dict.
Reproducibility comparisons must use `comparable(summary)`, which drops `fetched_at`.
"""
import datetime as dt
import hashlib
import http.client
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

VOLATILE_KEYS = ("fetched_at",)  # differ between identical re-runs; never compared
SUMMARY_KEYS = ["provider", "project", "window", "runs", "cancelled", "runs_per_day", "duration_min",
                "job_minutes_total", "failure_pct", "rerun_pct", "flaky_commits", "test_job_minutes",
                "full_run_share", "full_run_compute_share", "sources", "fetched_at", "method"]


class HTTPStatusError(Exception):
    def __init__(self, code, url):
        super().__init__("HTTP %s for %s" % (code, url))
        self.code = code


def http_get(url, headers=None, cache_dir=None, retries=4):
    """GET url -> (body text, response headers). Cached on disk by URL; network errors and 429/5xx retried with backoff."""
    key = os.path.join(cache_dir, hashlib.sha1(url.encode()).hexdigest() + ".json") if cache_dir else None
    if key and os.path.exists(key):
        try:
            with open(key) as f:
                c = json.load(f)
            return c["body"], c["headers"]
        except (ValueError, KeyError):  # torn write from an older, non-atomic writer: refetch
            os.remove(key)
    req = urllib.request.Request(url, headers={"User-Agent": "ci-casestudies-engine", **(headers or {})})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body, hdrs = resp.read().decode(), {k.lower(): v for k, v in resp.headers.items()}
            break
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == retries - 1:
                raise HTTPStatusError(e.code, url) from e
            wait = int(e.headers.get("Retry-After") or 10 * (attempt + 1))
        except (OSError, TimeoutError, http.client.HTTPException) as e:
            if attempt == retries - 1:
                raise
            wait = 5 * (attempt + 1)
        print("  retry %d in %ds: %s" % (attempt + 1, wait, url), file=sys.stderr)
        time.sleep(wait)
    if key:
        os.makedirs(cache_dir, exist_ok=True)
        tmp = "%s.%d.%d.tmp" % (key, os.getpid(), threading.get_ident())
        with open(tmp, "w") as f:
            json.dump({"url": url, "body": body, "headers": hdrs}, f)
        os.replace(tmp, key)  # atomic: parallel workers/processes share this cache
    return body, hdrs


def http_get_json(url, headers=None, cache_dir=None):
    return json.loads(http_get(url, headers, cache_dir)[0])


class Window:
    """The `days` full UTC days before `end` (end is exclusive), so re-runs see the same data."""

    def __init__(self, days, end):
        self.days = int(days)
        self.end = end if isinstance(end, dt.date) else dt.date.fromisoformat(end)
        self.start = self.end - dt.timedelta(days=self.days)

    def each_day(self):
        return [self.start + dt.timedelta(days=i) for i in range(self.days)]

    def contains(self, ts):
        return self.start <= parse_ts(ts).date() < self.end

    def as_dict(self):
        return {"start": self.start.isoformat(), "end_exclusive": self.end.isoformat(), "days": self.days}


def parse_ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def minutes(a, b):
    if not a or not b:
        return 0.0
    return max(0.0, (parse_ts(b) - parse_ts(a)).total_seconds() / 60)


def slug(project):
    return re.sub(r"[^A-Za-z0-9._-]+", "__", project.strip("/"))


def comparable(summary):
    """The summary without volatile keys: two runs on the same cached data must give equal results."""
    return {k: v for k, v in summary.items() if k not in VOLATILE_KEYS}


def run_started(r):
    """Run start (after queueing): the provider's value, else the earliest job start."""
    return r.get("started_at") or min((j["started_at"] for j in r["jobs"] if j.get("started_at")), default=None)


def _pct(a, b):
    return round(100.0 * a / b, 1) if b else None


def _p(xs, q):
    """Nearest-rank percentile."""
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[max(0, math.ceil(q * len(xs)) - 1)], 1)


def summarize(runs, window, provider, project, test_job_regex=None, full_run_rule=None,
              sources=None, fetched_at=None, method="", extra=None):
    """Common audit summary. `runs` counts completed runs (success + failure); cancelled are reported apart.

    failure_pct  = failed / runs; rerun_pct = runs with attempt > 1 / runs
    duration_min.from_created = finished - created (includes queueing);
    duration_min.from_started = finished - started (execution only; job-time basis as in HA Phase 0)
    flaky_commits = (workflow, commit) pairs with both a failed and a successful run: same code,
        same pipeline, different result. Grouping by commit alone would count "lint failed,
        tests passed" as flaky (anti-patterns.md).
    test_job_minutes = minutes of jobs whose name matches test_job_regex (None without a regex)
    full_run_share = runs having a job matching full_run_rule / runs (None without a rule)
    full_run_compute_share = test-job minutes in full runs / all test-job minutes
    """
    done = [r for r in runs if r["status"] in ("success", "failure")]
    test_re = re.compile(test_job_regex) if test_job_regex else None
    full_re = re.compile(full_run_rule) if full_run_rule else None
    by_commit = {}
    for r in done:
        by_commit.setdefault((r.get("workflow"), r.get("commit")), set()).add(r["status"])
    test_min = full_test_min = 0.0
    full_runs = 0
    for r in done:
        t = sum(minutes(j["started_at"], j["finished_at"]) for j in r["jobs"] if test_re and test_re.search(j["name"]))
        test_min += t
        if full_re and any(full_re.search(j["name"]) for j in r["jobs"]):
            full_runs += 1
            full_test_min += t
    n = len(done)
    queued = [minutes(r["created_at"], r["finished_at"]) for r in done]
    running = [minutes(run_started(r), r["finished_at"]) for r in done]
    out = {
        "provider": provider,
        "project": project,
        "window": window.as_dict(),
        "runs": n,
        "cancelled": sum(r["status"] == "cancelled" for r in runs),
        "runs_per_day": round(n / window.days, 1),
        "duration_min": {"from_created": {"median": _p(queued, 0.5), "p90": _p(queued, 0.9)},
                         "from_started": {"median": _p(running, 0.5), "p90": _p(running, 0.9)}},
        "job_minutes_total": round(sum(minutes(j["started_at"], j["finished_at"]) for r in done for j in r["jobs"]), 1),
        "failure_pct": _pct(sum(r["status"] == "failure" for r in done), n),
        "rerun_pct": _pct(sum((r.get("attempt") or 1) > 1 for r in done), n),
        "flaky_commits": sum(1 for (_, c), s in by_commit.items() if c and s == {"success", "failure"}),
        "test_job_minutes": round(test_min, 1) if test_re else None,
        "full_run_share": _pct(full_runs, n) if full_re else None,
        "full_run_compute_share": _pct(full_test_min, test_min) if (full_re and test_re) else None,
        "sources": sources or [],
        "fetched_at": fetched_at or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "method": method,
    }
    if extra:
        out["extra"] = extra
    return out


def breakdown(runs, test_job_regex=None, full_run_rule=None, every_run_pct=90.0):
    """Waste and critical-path detail for the audit report (goes into the summary's `extra`).

    run_kinds.<full|partial> (or .all without a rule), completed runs only:
        runs; wall_min = created -> finished (what a developer waits, queue included) median/p90/total;
        exec_min = started -> finished median/p90; test_job_min / job_min totals;
        longest_job_min median (the single slowest job per run: the floor of the wall time when jobs run in parallel)
    every_run_jobs = jobs (by name) present in >= every_run_pct % of runs: runs_pct, job_min total, median
    last_job = per run kind, the job that finishes last (the end of the critical path): name -> runs
    """
    done = [r for r in runs if r["status"] in ("success", "failure")]
    test_re = re.compile(test_job_regex) if test_job_regex else None
    full_re = re.compile(full_run_rule) if full_run_rule else None
    kinds, last = {}, {}
    for r in done:
        k = ("full" if any(full_re.search(j["name"]) for j in r["jobs"]) else "partial") if full_re else "all"
        kinds.setdefault(k, []).append(r)
    out = {}
    for k, rs in sorted(kinds.items()):
        wall = [minutes(r["created_at"], r["finished_at"]) for r in rs]
        execm = [minutes(run_started(r), r["finished_at"]) for r in rs]
        longest = [max([minutes(j["started_at"], j["finished_at"]) for j in r["jobs"]] or [0.0]) for r in rs]
        out[k] = {"runs": len(rs),
                  "wall_min": {"median": _p(wall, 0.5), "p90": _p(wall, 0.9), "total": round(sum(wall), 1)},
                  "exec_min": {"median": _p(execm, 0.5), "p90": _p(execm, 0.9)},
                  "test_job_min": round(sum(minutes(j["started_at"], j["finished_at"]) for r in rs for j in r["jobs"]
                                            if test_re and test_re.search(j["name"])), 1) if test_re else None,
                  "job_min": round(sum(minutes(j["started_at"], j["finished_at"]) for r in rs for j in r["jobs"]), 1),
                  "longest_job_min": {"median": _p(longest, 0.5)}}
        c = {}
        for r in rs:
            ends = [j for j in r["jobs"] if j.get("finished_at")]
            if ends:
                n = max(ends, key=lambda j: parse_ts(j["finished_at"]))["name"]
                c[n] = c.get(n, 0) + 1
        last[k] = sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    per_job = {}
    for r in done:
        for name in {j["name"] for j in r["jobs"]}:
            per_job.setdefault(name, []).append(sum(minutes(j["started_at"], j["finished_at"]) for j in r["jobs"] if j["name"] == name))
    every = [{"job": n, "runs_pct": _pct(len(ms), len(done)), "job_min": round(sum(ms), 1), "median_min": _p(ms, 0.5)}
             for n, ms in per_job.items() if done and 100.0 * len(ms) / len(done) >= every_run_pct]
    every.sort(key=lambda x: (-x["job_min"], x["job"]))
    return {"run_kinds": out, "every_run_jobs": every, "last_job": last, "every_run_pct_threshold": every_run_pct}


def test_execution(runs, test_job_regex=None, min_test_min=1.0):
    """Split completed runs into those where a test job actually ran and those where none did (e.g. skipped by a
    paths filter: the adapter drops skipped jobs). Without this, fast no-op runs drag the median down
    (a busy repo can show a 0.7 min median while real test runs take 10-14 min).
    -> {min_test_min, runs_with_tests, runs_without_tests, with_tests: {exec median/p90}, without_tests: {...}}; None without a regex."""
    if not test_job_regex:
        return None
    test_re = re.compile(test_job_regex)
    done = [r for r in runs if r["status"] in ("success", "failure")]
    # "ran tests" = its test-named jobs add up to >= min_test_min: gate jobs named "test" that exit in seconds
    # after a change check (seen: 0.1-0.2 min) don't count as running tests
    tmin = lambda r: sum(minutes(j["started_at"], j["finished_at"]) for j in r["jobs"] if test_re.search(j["name"]))
    yes = [r for r in done if tmin(r) >= min_test_min]
    no = [r for r in done if r not in yes]
    stat = lambda rs: {"median": _p([minutes(run_started(r), r["finished_at"]) for r in rs], 0.5),
                       "p90": _p([minutes(run_started(r), r["finished_at"]) for r in rs], 0.9)}
    return {"min_test_min": min_test_min, "runs_with_tests": len(yes), "runs_without_tests": len(no),
            "with_tests_exec_min": stat(yes), "without_tests_exec_min": stat(no),
            "failure_pct_with_tests": _pct(sum(r["status"] == "failure" for r in yes), len(yes))}
