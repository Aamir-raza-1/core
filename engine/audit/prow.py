"""Prow audit adapter (kubernetes-style public Prow + GCS job artifacts), read-only and cached.

  python3 -m engine.cli audit --provider prow --project kubernetes/kubernetes --job pull-kubernetes-unit --days 7 --end 2026-10-11

Run list: the GCS JSON API listing of `pr-logs/directory/<job>/<build>.txt` in the public bucket. Each object
carries `metadata.x-goog-meta-link` = gs://<bucket>/pr-logs/pull/<pr>/<job>/<build> (batch runs live under
pr-logs/pull/batch/ and are skipped: they test several PRs at once). Build ids are Prow snowflakes
(ms since the Twitter epoch << 22), so the window becomes a `startOffset`/`endOffset` name range
(12 h margin before the window start, for runs queued before it), no full listing needed. Retention ~90 days.
Per run: started.json (`timestamp`, `repos` = "<base ref>:<base sha>,<pr>:<head sha>") and finished.json
(`timestamp`, `result`). Status: SUCCESS -> success, FAILURE -> failure, ABORTED -> cancelled,
ERROR (pod/infra error) -> error (not counted, like cancelled); results are matched case-insensitively
(finished.json written by crier instead of the sidecar has "success"/"failure"); no finished.json -> still running, dropped.
A run is kept when its started timestamp falls in the window. A Prow presubmit has no run -> jobs nesting:
each run is one job, `attempt` = its rank among runs of the same (PR, head sha) by start time, so
`flaky_commits` (common) = same PR head commit failed and passed (/retest).
`commit` = PR head sha; `base_sha` is kept too (a /retest merges the then-current base).
Junit (per run, on demand for shadow/flaky): artifacts/junit*.xml parsed with engine.flaky.go_junit_detail;
the raw XML (~600 KB) is not kept, only the parsed result (cache_dir/junit-v2/<build>.json).
"""
import concurrent.futures
import datetime as dt
import json
import os
import re
import threading
import urllib.parse

from . import common

BUCKET = "kubernetes-ci-logs"
LIST = "https://storage.googleapis.com/storage/v1/b/%s/o?%s"
OBJ = "https://storage.googleapis.com/%s/%s"
EPOCH_MS = 1288834974657  # Prow build ids are Twitter-epoch snowflakes
SOURCES = ["GCS JSON API (public, anonymous): storage/v1/b/kubernetes-ci-logs/o?prefix=pr-logs/directory/<job>/ "
           "(+ per run pr-logs/pull/<pr>/<job>/<build>/{started,finished}.json, artifacts/junit*.xml)"]
METHOD = ("Prow presubmit runs of one job whose started.json timestamp is in the window (batch runs skipped); "
          "one job per run; attempt = rank among runs of the same (PR, head sha); SUCCESS|FAILURE counted, "
          "ABORTED = cancelled, ERROR = not counted")
WORKERS = 32  # GCS object reads are latency-bound (~0.4 s each)
STATUS = {"SUCCESS": "success", "FAILURE": "failure", "ABORTED": "cancelled", "ERROR": "error"}


def build_id_at(t):
    """Smallest snowflake build id created at datetime t (UTC)."""
    return (int(t.timestamp() * 1000) - EPOCH_MS) << 22


def build_time(build):
    return dt.datetime.fromtimestamp(((int(build) >> 22) + EPOCH_MS) / 1000, dt.timezone.utc)


def _iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _frozen(window):
    """Cache listings only for windows that ended (all runs created before the end are listed)."""
    return window.end <= dt.datetime.now(dt.timezone.utc).date()


def list_builds(job, window, cache_dir):
    """[(build id, gcs prefix)] of PR runs created in [window start - 12 h, window end)."""
    prefix = "pr-logs/directory/%s/" % job
    lo = build_id_at(dt.datetime.combine(window.start, dt.time(), dt.timezone.utc) - dt.timedelta(hours=12))
    hi = build_id_at(dt.datetime.combine(window.end, dt.time(), dt.timezone.utc))
    out, token = [], None
    while True:
        q = {"prefix": prefix, "startOffset": "%s%d" % (prefix, lo), "endOffset": "%s%d" % (prefix, hi),
             "maxResults": "1000", "fields": "items(name,metadata),nextPageToken"}
        if token:
            q["pageToken"] = token
        page = common.http_get_json(LIST % (BUCKET, urllib.parse.urlencode(q)), cache_dir=cache_dir if _frozen(window) else None)
        for it in page.get("items", []):
            link = (it.get("metadata") or {}).get("x-goog-meta-link", "")
            m = re.match(r"gs://[^/]+/(pr-logs/pull/(\d+)/[^/]+/(\d+))/?$", link)
            if m:  # batch runs (pr-logs/pull/batch/...) do not match
                out.append((m.group(3), m.group(1), int(m.group(2))))
        token = page.get("nextPageToken")
        if not token:
            return out


def _run(job, build, gcs, pr, cache_dir):
    try:
        st = common.http_get_json(OBJ % (BUCKET, gcs + "/started.json"), cache_dir=cache_dir)
    except common.HTTPStatusError as e:
        if e.code == 404:
            return None
        raise
    try:
        fin = common.http_get_json(OBJ % (BUCKET, gcs + "/finished.json"), cache_dir=cache_dir)
    except common.HTTPStatusError as e:
        if e.code == 404:  # still running (or the pod never reported): not finished, dropped
            return None
        raise
    repos = (st.get("repos") or {}).get("kubernetes/kubernetes") or next(iter((st.get("repos") or {}).values()), "")
    base_sha = head_sha = None
    for part in repos.split(","):
        k, _, sha = part.partition(":")
        if k == str(pr):
            head_sha = sha
        elif sha and base_sha is None:
            base_sha = sha
    status = STATUS.get((fin.get("result") or "").upper(), "error")  # crier writes lowercase results
    s, f = _iso(st["timestamp"]), _iso(fin["timestamp"])
    return {"id": build, "workflow": job, "created_at": build_time(build).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "started_at": s, "finished_at": f, "status": status, "attempt": 1, "commit": head_sha or fin.get("revision"),
            "ref_kind": "pr", "jobs": [{"name": job, "started_at": s, "finished_at": f, "status": status}],
            "pr": pr, "base_sha": base_sha, "gcs_prefix": gcs, "result": fin.get("result")}


def fetch_runs(project, window, cache_dir, job=None, **_):
    if not job:
        raise ValueError("prow: give --job (a Prow job name, e.g. pull-kubernetes-unit)")
    builds = list_builds(job, window, cache_dir)
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
        runs = [r for r in ex.map(lambda b: _run(job, b[0], b[1], b[2], cache_dir), builds) if r]
    runs = [r for r in runs if window.contains(r["started_at"])]
    runs.sort(key=lambda r: (r["started_at"], r["id"]))
    seen = {}
    for r in runs:
        k = (r["pr"], r["commit"])
        seen[k] = seen.get(k, 0) + 1
        r["attempt"] = seen[k]
    return runs


def junit(run, cache_dir):
    """Parsed junit of one run: {files, packages: {import path: s}, failed: [{package, tests}]}; files=0 = no junit."""
    from engine import flaky  # noqa: PLC0415  (flaky imports engine.audit.common; keep the audit import light)
    path = os.path.join(cache_dir, "junit-v2", "%s.json" % run["id"])  # bump with engine.flaky.go_junit_detail
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    q = {"prefix": run["gcs_prefix"] + "/artifacts/", "fields": "items(name,size),nextPageToken", "maxResults": "1000"}
    items = common.http_get_json(LIST % (BUCKET, urllib.parse.urlencode(q)), cache_dir=cache_dir).get("items", [])
    names = sorted(i["name"] for i in items if re.search(r"/junit[^/]*\.xml$", i["name"]))
    out = {"files": len(names), "packages": {}, "failed": []}
    for n in names:
        d = flaky.go_junit_detail(common.http_get(OBJ % (BUCKET, n))[0])
        for p, t in d["packages"].items():
            out["packages"][p] = round(out["packages"].get(p, 0.0) + t, 3)
        out["failed"] += d["failed"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    with open(tmp, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    os.replace(tmp, path)
    return out


def shadow_runs(project, window, cache_dir, job, statuses=("failure",), with_junit=True):
    """Run source for passive shadow (engine.cli shadow / experiments/k8s/shadow_k8s.py): yields
    {run_id, base_sha, head_sha, failed_test_ids, status} (+ pr, started_at, attempt, junit_files, failed_packages)
    for completed runs whose status is in `statuses` (None = all). failed_test_ids = `<import path>::<Test>`
    (`<import path>` alone when the junit names no test); junit_files = 0 means the run left no junit."""
    runs = [r for r in fetch_runs(project, window, cache_dir, job=job) if statuses is None or r["status"] in statuses]
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
        js = list(ex.map(lambda r: junit(r, cache_dir) if with_junit else None, runs))
    for r, j in zip(runs, js):
        failed = (j or {}).get("failed", [])
        ids = sorted({"%s::%s" % (f["package"], t) for f in failed for t in f["tests"]} | {f["package"] for f in failed if not f["tests"]})
        yield {"run_id": r["id"], "base_sha": r["base_sha"], "head_sha": r["commit"], "failed_test_ids": ids,
               "status": r["status"], "pr": r["pr"], "started_at": r["started_at"], "finished_at": r["finished_at"],
               "attempt": r["attempt"], "junit_files": (j or {}).get("files"), "failed_packages": failed,
               "gcs_prefix": r["gcs_prefix"]}


GH_COMPARE = "https://api.github.com/repos/%s/compare/%s...%s?per_page=100&page=%d"


def repo_dir(import_path, module="k8s.io/kubernetes", staging="staging/src/"):
    """Go import path of a junit package -> repo dir (the Go selection unit). Main module `k8s.io/kubernetes/x` -> x;
    other `k8s.io/<m>/...` are the staging modules -> staging/src/k8s.io/<m>/... (kubernetes' layout, the only Prow
    repo here). None for packages outside the workspace: hack/tools modules and vendored third-party code (their own
    junit files; hack/dependency-unit-tests.conf)."""
    if import_path.startswith(module + "/"):
        d = import_path[len(module) + 1:]
        return None if d.startswith("hack/tools/") else d
    if import_path.startswith("k8s.io/"):
        return staging + import_path
    return None


def changed_files(project, base, head, cache_dir):
    """Exact tested diff, GitHub compare {base}...{head} (three-dot: from the merge base), paged past the 300-file cap;
    renames add both paths. Only filenames are cached (cache_dir/compare/), the raw response carries patches."""
    path = os.path.join(cache_dir, "compare", "%s...%s.json" % (base, head))
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    hdr = {"Accept": "application/vnd.github+json", **({"Authorization": "Bearer " + tok} if tok else {})}
    files, page = [], 1
    while True:
        try:
            body = json.loads(common.http_get(GH_COMPARE % (project, base, head, page), hdr)[0])
        except common.HTTPStatusError as e:
            out = {"error": e.code, "files": None}
            break
        batch = body.get("files") or []
        for f in batch:
            files.append(f["filename"])
            if f.get("previous_filename"):
                files.append(f["previous_filename"])
        if len(batch) < 100 or page >= 30:
            out = {"files": sorted(set(files)), "pages": page, "commits": body.get("total_commits")}
            break
        page += 1
    if out.get("files") is not None or out.get("error") == 404:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
        with open(tmp, "w") as f:
            json.dump(out, f)
        os.replace(tmp, path)
    return out


def shadow_source(project, window, cache_dir, workflows=None, **_):
    """engine.shadow SOURCES entry: `engine.cli shadow --provider prow --workflow <job>` (the Prow job name).
    Records carry failed_test_ids as `<repo dir>::<TestName>` (`<repo dir>` alone without a test name), the dir
    being the Go selection unit; failures outside the workspace (hack/tools, vendor junits) are left out of the ids
    and listed in `out_of_workspace`. Runs without junit keep empty ids (setup-only). changed_fn = exact compare."""
    job = (workflows or [None])[0]
    if not job:
        raise ValueError("shadow --provider prow: give the Prow job as --workflow, e.g. pull-kubernetes-unit")
    recs = []
    for r in shadow_runs(project, window, cache_dir, job, statuses=None, with_junit=False):
        recs.append(r)
    fails = [r for r in recs if r["status"] == "failure"]
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
        js = dict(zip([r["run_id"] for r in fails], ex.map(lambda r: junit({"id": r["run_id"], "gcs_prefix": r["gcs_prefix"]}, cache_dir), fails)))
    out = []
    for r in recs:
        ids, outside = set(), []
        for f in js.get(r["run_id"], {}).get("failed", []):
            d = repo_dir(f["package"])
            if d is None:
                outside.append(f["package"])
            else:
                ids |= {"%s::%s" % (d, t) for t in f["tests"]} or {d}
        out.append({"run_id": r["run_id"], "attempt": r["attempt"], "workflow": job, "pr": r["pr"],
                    "created_at": r["started_at"], "base_sha": r["base_sha"], "head_sha": r["head_sha"],
                    "status": r["status"] if r["status"] in ("success", "failure") else "other",
                    "failed_test_ids": sorted(ids), "out_of_workspace": outside})

    def changed_fn(rec):
        return changed_files(project, rec["base_sha"], rec["head_sha"], cache_dir).get("files"), rec["base_sha"]
    return out, changed_fn
