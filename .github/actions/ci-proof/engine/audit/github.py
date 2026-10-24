"""GitHub Actions audit adapter: pull-request workflow runs + their jobs (read-only, cached).

Runs: one query per UTC day (`created=<day>`); the API caps any single query at 1,000 results, so a
day whose total_count exceeds that is re-read in bisected time ranges (`created=<ts>..<ts>`),
events pull_request and pull_request_target, status completed. `workflows` limits the runs to
named workflow files (e.g. ["ci.yaml"]); without it, all workflows are read.
Jobs: fetched only for runs concluding success|failure (the only ones the summary counts), latest attempt (`filter=latest`); skipped jobs are dropped, so a job that
appears only in full runs (HA: "Split tests for full run") can feed --full-run-rule.
Conclusions: success / failure kept (unless the latest attempt has no jobs at all: status no_jobs), cancelled reported apart, anything else (skipped, neutral,
timed_out, action_required) gets its own status and is not counted as a run (as in HA Phase 0).
Token: GH_TOKEN or GITHUB_TOKEN (public repos work without one, at 60 requests/hour).
"""
import concurrent.futures
import datetime as dt
import os

from . import common

API = "https://api.github.com"
EVENTS = ("pull_request", "pull_request_target")
SOURCES = ["GitHub REST API: /actions/runs (per day, per event), /actions/runs/{id}/jobs?filter=latest"]
METHOD = "PR workflow runs (pull_request, pull_request_target), latest attempt's non-skipped jobs; conclusion success|failure counted"
WORKERS = 8  # job lists are fetched in parallel; GitHub's secondary rate limits tolerate this


def _headers():
    h = {"Accept": "application/vnd.github+json"}
    tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if tok:
        h["Authorization"] = "Bearer " + tok
    return h


CAP = 1000  # GitHub returns at most 1,000 results for any one runs query (10 pages of 100)


def _runs_url(project, workflow, event, created, page):
    base = "%s/repos/%s/actions/%sruns" % (API, project, "workflows/%s/" % workflow if workflow else "")
    return "%s?event=%s&status=completed&per_page=100&page=%d&created=%s" % (base, event, page, created)


def _ts(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _query(project, workflow, event, created, cache_dir, out):
    """All runs for one `created` filter; returns total_count. Callers split the range when it exceeds CAP."""
    page, total = 1, 0
    while True:
        data = common.http_get_json(_runs_url(project, workflow, event, created, page), _headers(), cache_dir)
        total = data.get("total_count", 0)
        if page == 1 and total > CAP:
            return total  # truncated: don't page, the caller splits the range
        batch = data.get("workflow_runs", [])
        for r in batch:
            out[r["id"]] = r
        if len(batch) < 100:
            return total
        page += 1


def _range(project, workflow, event, start, end, cache_dir, out):
    """Runs created in [start, end] (inclusive, datetimes); bisected while a query would exceed CAP."""
    created = "%s..%s" % (_ts(start), _ts(end))
    if _query(project, workflow, event, created, cache_dir, out) <= CAP:
        return
    if (end - start).total_seconds() < 60:
        raise ValueError("more than %d runs created within one minute (%s): cannot split further" % (CAP, created))
    mid = start + (end - start) / 2
    mid = mid.replace(microsecond=0)
    _range(project, workflow, event, start, mid, cache_dir, out)
    _range(project, workflow, event, mid + dt.timedelta(seconds=1), end, cache_dir, out)


def _runs(project, window, cache_dir, workflows):
    out = {}
    for day in window.each_day():
        for wf in workflows or [None]:
            for event in EVENTS:
                if _query(project, wf, event, day.isoformat(), cache_dir, out) > CAP:  # busy day: split by time
                    start = dt.datetime.combine(day, dt.time(0, 0, 0))
                    _range(project, wf, event, start, start + dt.timedelta(seconds=86399), cache_dir, out)
    return [r for r in out.values() if window.contains(r["created_at"])]


def _jobs(project, run_id, cache_dir):
    jobs, page = [], 1
    while True:
        data = common.http_get_json("%s/repos/%s/actions/runs/%d/jobs?filter=latest&per_page=100&page=%d"
                                    % (API, project, run_id, page), _headers(), cache_dir)
        jobs += data["jobs"]
        if len(jobs) >= data["total_count"] or not data["jobs"]:
            return jobs
        page += 1


def _status(conclusion):
    return conclusion if conclusion in ("success", "failure", "cancelled") else (conclusion or "unknown")


def fetch_runs(project, window, cache_dir, workflows=None, **_):
    runs = _runs(project, window, cache_dir, workflows)
    counted = [r for r in runs if _status(r.get("conclusion")) in ("success", "failure")]  # only these feed job metrics
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
        all_jobs = dict(zip([r["id"] for r in counted], ex.map(lambda r: _jobs(project, r["id"], cache_dir), counted)))
    out = []
    for r in sorted(runs, key=lambda r: (r["created_at"], r["id"])):
        out.append({
            "id": r["id"],
            "workflow": r.get("path") or r.get("name"),
            "created_at": r["created_at"],
            "started_at": r.get("run_started_at"),
            "finished_at": r.get("updated_at"),
            # a run whose latest attempt has no jobs at all never executed (e.g. a fork PR awaiting approval that
            # GitHub later closes as "failure"); it is not a CI run, so it gets its own status and is not counted
            "status": "no_jobs" if r["id"] in all_jobs and not all_jobs[r["id"]] else _status(r.get("conclusion")),
            "attempt": r.get("run_attempt") or 1,
            "commit": r.get("head_sha"),
            "ref_kind": "pr",
            "jobs": [{"name": j["name"], "started_at": j.get("started_at"), "finished_at": j.get("completed_at"),
                      "status": _status(j.get("conclusion"))}
                     for j in all_jobs.get(r["id"], []) if j.get("conclusion") not in ("skipped", None)],
        })
    return out
