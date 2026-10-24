"""GitLab CI audit adapter: merge-request pipelines + their jobs (read-only, cached).

Pipelines: `/projects/:id/pipelines?source=merge_request_event` filtered server-side by
`updated_after` / `updated_before` (a pipeline is updated after it is created, so the upper bound gets
2 extra days) and client-side by `created_at` in the window. Paged with `per_page=100`.
Jobs: `/projects/:id/pipelines/:pid/jobs?include_retried=true`, fetched only for pipelines concluding
success|failed. A job GitLab retried shows up twice with the same name (no `retried` flag in the
list response); the highest id is the latest copy, the run's `attempt` is 1 + the most retries of any
one job, and only the latest copy of each job counts.
Run start/finish come from the jobs (earliest start, latest finish), so no per-pipeline detail call.
Duration is therefore the wall-clock span of the counted jobs; GitLab's own `duration` counts only time
when jobs were running, so it can be shorter when stages wait (spot-check 2026-10-11: 15.0 vs 15.0,
124.9 vs 124.7, 22.2 vs 17.2 min).
Statuses: success, failed -> failure, canceled -> cancelled, skipped -> skipped (not counted);
running/pending/created/manual/scheduled/waiting_for_resource are not finished and are dropped.
Token: GITLAB_TOKEN (optional; public projects work without one, rate-limited).
Project id: the URL-encoded path (`group/project`) or the numeric id.
"""
import concurrent.futures
import datetime as dt
import os
import urllib.parse

from . import common

API = "https://gitlab.com/api/v4"
SOURCES = ["GitLab REST API v4: /projects/:id/pipelines?source=merge_request_event, /projects/:id/pipelines/:pid/jobs?include_retried=true"]
METHOD = ("merge-request pipelines created in the window; latest copy of each non-skipped job; "
          "attempt = 1 + max retries of a job; status success|failed counted")
WORKERS = 6
STATUS = {"success": "success", "failed": "failure", "canceled": "cancelled", "skipped": "skipped"}


def _headers():
    tok = os.environ.get("GITLAB_TOKEN")
    return {"PRIVATE-TOKEN": tok} if tok else {}


def _pid(project):
    return project if project.isdigit() else urllib.parse.quote(project, safe="")


def _iso(d):
    return dt.datetime.combine(d, dt.time(0, 0)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pipelines(project, window, cache_dir):
    out, page = [], 1
    after, before = _iso(window.start), _iso(window.end + dt.timedelta(days=2))
    while True:
        url = ("%s/projects/%s/pipelines?source=merge_request_event&updated_after=%s&updated_before=%s"
               "&order_by=id&sort=desc&per_page=100&page=%d" % (API, _pid(project), after, before, page))
        batch = common.http_get_json(url, _headers(), cache_dir) or []
        out += batch
        if len(batch) < 100:
            break
        page += 1
    return [p for p in out if window.contains(p["created_at"])]


def _jobs(project, pipeline_id, cache_dir):
    jobs, page = [], 1
    while True:
        url = "%s/projects/%s/pipelines/%d/jobs?include_retried=true&per_page=100&page=%d" % (API, _pid(project), pipeline_id, page)
        batch = common.http_get_json(url, _headers(), cache_dir) or []
        jobs += batch
        if len(batch) < 100:
            return jobs
        page += 1


def normalize(p, jobs):
    """One GitLab pipeline + its jobs (incl. retried copies) -> a normalized run (see common.py).

    The jobs API returns retried copies as ordinary jobs with the same name (there is no `retried`
    field in the list response, checked 2026-10-11 on 253k jobs): per name, the highest id is the
    latest copy; the others are earlier attempts."""
    by_name = {}
    for j in jobs:
        by_name.setdefault(j["name"], []).append(j)
    retries = {n: len(js) - 1 for n, js in by_name.items() if len(js) > 1}
    newest = [max(js, key=lambda j: j["id"]) for js in by_name.values()]
    latest = [j for j in newest if STATUS.get(j.get("status")) in ("success", "failure", "cancelled")]
    starts = [j["started_at"] for j in latest if j.get("started_at")]
    ends = [j["finished_at"] for j in latest if j.get("finished_at")]
    return {
        "id": p["id"],
        "workflow": p.get("name") or p.get("source") or "pipeline",
        "created_at": p["created_at"],
        "started_at": min(starts) if starts else None,
        "finished_at": max(ends) if ends else p.get("updated_at"),
        "status": STATUS.get(p.get("status"), p.get("status") or "unknown"),
        "attempt": 1 + max(retries.values(), default=0),
        "commit": p.get("sha"),
        "ref_kind": "pr",
        "jobs": [{"name": j["name"], "started_at": j.get("started_at"), "finished_at": j.get("finished_at"),
                  "status": STATUS[j["status"]]} for j in latest],
    }


def fetch_runs(project, window, cache_dir, workflows=None, **_):
    pipes = [p for p in _pipelines(project, window, cache_dir) if p.get("status") in STATUS]
    if workflows:
        pipes = [p for p in pipes if (p.get("name") or p.get("source")) in workflows]
    counted = [p for p in pipes if STATUS[p["status"]] in ("success", "failure")]
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
        jobs = dict(zip([p["id"] for p in counted], ex.map(lambda p: _jobs(project, p["id"], cache_dir), counted)))
    return [normalize(p, jobs.get(p["id"], [])) for p in sorted(pipes, key=lambda p: (p["created_at"], p["id"]))]
