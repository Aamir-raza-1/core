"""Flaky CI detection, failing-test parsing, cost and evidence-based categories (AF1).

Definition (anti-patterns.md "false flaky"): flaky = within one (workflow, commit), a failed
attempt followed by a passed attempt with no code change. Two kinds:
  rerun    - a later attempt of the same run passed (the usual "re-run failed jobs")
  new-run  - a later, separate run of the same workflow on the same commit passed
"lint failed, tests passed" on one commit is two workflows, so it is never flaky.

Pipeline (each step pure except the provider fetch):
  attempts = github_attempts(...)           # one record per attempt (provider adapter part)
  episodes = detect(attempts)               # flaky episodes
  report   = aggregate(episodes, pr_runs, jobs_fn, log_fn, ...)

Attempt record: {run_id, attempt, workflow, commit, status: success|failure|cancelled|no_jobs|...,
                 created_at, started_at, finished_at}
Job record (from jobs_fn(attempt)): {id, name, status, started_at, finished_at}
log_fn(job) -> raw log text or None (logs are parsed, never kept; only parsed results are cached).

Costs are estimates (labelled): extra job-minutes = job time of the failed attempts in each
episode; wait-minutes = started -> finished of those failed attempts (time lost before the pass).
"""
import collections
import concurrent.futures
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from engine.audit import common

PARSE_VERSION = 1  # bump when every parser changes: invalidates all cached parsed logs
PARSER_VERSIONS = {"pytest": 1, "vitest": 2, "jest": 2}  # bump one parser: only logs parsed with it are re-read
WORKERS = 8
DEFAULT_TEST_JOB_REGEX = r"(?i)test"

# ------------------------------------------------------------------------------------- parsing

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def strip_ansi(text):
    return ANSI.sub("", text)


_PYTEST = re.compile(r"(?:^|\s)(?:FAILED|ERROR)\s+([^\s:]+?\.py(?:::[^\s]+)?)(?:\s+-\s+|\s*$)")


def parse_pytest(text):
    """pytest short summary / verbose lines: `FAILED path/test_x.py::test_y - msg`, `ERROR path.py::t`."""
    return sorted({m.group(1) for line in text.splitlines() for m in [_PYTEST.search(line)] if m})


_JS_FILE = r"([^\s>]+\.(?:test|spec)\.[cm]?[jt]sx?)"
# optional project label between FAIL and the file: vitest workspaces print `FAIL  |name| file`, jest `projects`
# with a displayName print `FAIL name file` (one bare word; a path or a test file is never taken as the label)
_VITEST = re.compile(r"(?:^|\s)FAIL\s+(?:\|[^|\s]+\|\s+)?" + _JS_FILE + r"\s+>\s+(.+?)\s*$")
_JEST_FILE = re.compile(r"(?:^|\s)FAIL\s+(?:[A-Za-z][\w-]*\s+)?" + _JS_FILE + r"(?:\s|$)")
_JEST_TEST = re.compile(r"^\s*(?:\S+Z\s+)?●\s+(.+?)\s*$")


def parse_vitest_jest(text):
    """Vitest `FAIL  file.test.ts > suite > test` -> `file > suite > test`; Jest `FAIL file.test.ts`
    + `● suite › test` -> `file > suite › test` (or the file alone if no test line follows)."""
    out, current = set(), None
    for line in text.splitlines():
        m = _VITEST.search(line)
        if m:
            out.add("%s > %s" % (m.group(1), m.group(2)))
            continue
        m = _JEST_FILE.search(line)
        if m:
            current = m.group(1)
            out.add(current)
            continue
        m = _JEST_TEST.match(line)
        if m and current and not m.group(1).startswith("Console"):
            out.discard(current)
            out.add("%s > %s" % (current, m.group(1)))
    return sorted(out)


def parse_junit(xml_text):
    """JUnit XML: every <testcase> with a <failure> or <error> child -> `file-or-classname::name`."""
    root = ET.fromstring(xml_text)
    out = set()
    for tc in root.iter("testcase"):
        if any(ch.tag in ("failure", "error") for ch in tc):
            where = tc.get("file") or tc.get("classname") or ""
            out.add("%s::%s" % (where, tc.get("name", "")) if where else tc.get("name", ""))
    return sorted(out)


_GO_FAIL = re.compile(r"--- FAIL: (\S+)")
_GO_RUN = re.compile(r"=== RUN\s+(\S+)")
_GO_TEST_NAME = re.compile(r"^(Test|Example|Fuzz|Benchmark)([A-Z0-9_]|$)")
_SIG = re.compile(r"^\[[^\]]*\]\s*")


def go_junit_detail(xml_text):
    """Go junit from gotestsum (kubernetes' unit job) -> {packages: {import path: seconds}, failed: [{package, tests}]}.

    Two shapes occur in the same job (both seen on real runs, 2026-10):
      pruned    one <testcase> per package: classname '[sig-x] <parent import path>', name '<leaf>'; failing
                top-level test names come from `--- FAIL: TestX` in the failure text (subtests folded into
                their parent), else the tests still `=== RUN` (timeout panic), else [] (build failure)
      per-test  <testsuite name='[sig-x] <import path>'> with one <testcase name='TestX[/sub]'> per test;
                a build failure is a testcase named TestMain with an empty classname
    Seconds per package = the pruned testcase time, or the per-test suite's time."""
    root = ET.fromstring(xml_text)
    pkgs, failed = {}, {}
    for suite in root.iter("testsuite"):
        cases = suite.findall("testcase")
        per_test = any(_GO_TEST_NAME.match(tc.get("name", "")) for tc in cases)
        if per_test:
            pkg = _SIG.sub("", suite.get("name") or "")
            pkgs[pkg] = round(pkgs.get(pkg, 0.0) + float(suite.get("time") or 0), 3)
        for tc in cases:
            if not per_test:
                cls = _SIG.sub("", tc.get("classname") or "")
                pkg = "%s/%s" % (cls, tc.get("name", "")) if cls else tc.get("name", "")
                pkgs[pkg] = round(pkgs.get(pkg, 0.0) + float(tc.get("time") or 0), 3)
            bad = [ch for ch in tc if ch.tag in ("failure", "error")]
            if not bad:
                continue
            text = "".join((ch.text or "") + " " + (ch.get("message") or "") for ch in bad)
            if per_test:
                tests = {tc.get("name", "").split("/")[0]}
            else:
                tests = {t.split("/")[0] for t in _GO_FAIL.findall(text)} or {t.split("/")[0] for t in _GO_RUN.findall(text)}
            failed.setdefault(pkg, set()).update(tests)
    return {"packages": pkgs, "failed": [{"package": p, "tests": sorted(t)} for p, t in failed.items()]}


def parse_go_junit(xml_text):
    """Go package-level junit -> failed `package::TestName` ids (`package` alone when no test name is known)."""
    out = set()
    for f in go_junit_detail(xml_text)["failed"]:
        out |= {"%s::%s" % (f["package"], t) for t in f["tests"]} or {f["package"]}
    return sorted(out)


LOG_PARSERS = {"pytest": parse_pytest, "vitest": parse_vitest_jest, "jest": parse_vitest_jest}
PARSERS = dict(LOG_PARSERS, junit=parse_junit, gojunit=parse_go_junit)  # junit/gojunit take report XML, not a job log

# Evidence patterns (generic CI vocabulary; repo rules belong in profiles).
TIMEOUT_STRICT = re.compile(r"\+{3,}\s*Timeout\s*\+{3,}|Failed: Timeout >|exceeded the maximum execution time"
                            r"|timed out after \d|Timeout of \d+\s*ms exceeded|Exceeded timeout of \d+\s*ms", re.I)
TIMEOUT_MENTION = re.compile(r"Timeout|TimeoutError|timed out", re.I)
INFRA_LOG = re.compile(r"ECONNRESET|ETIMEDOUT|Connection reset by peer|Could not resolve host|rate limit exceeded"
                       r"|HTTP(?:/[\d.]+)? 5\d\d|No space left on device|lost communication with the server"
                       r"|runner has received a shutdown signal|Service Unavailable|Bad Gateway", re.I)
LINT_JOB = re.compile(r"(?i)lint|mypy|typecheck|type-check|type check|tsc\b|ruff|flake8|prettier|format|style")
INFRA_JOB = re.compile(r"(?i)upload|codecov|coverage report|artifact|cache|deploy|publish|docker|setup|install|checkout")


def analyze_log(text, parsers=("pytest", "vitest")):
    """Raw job log -> small parsed result (the raw log is never stored)."""
    text = strip_ansi(text)
    tests = set()
    for p in parsers:
        tests.update(LOG_PARSERS[p](text))
    infra = INFRA_LOG.search(text)
    return {"ok": True, "tests": sorted(tests), "timeout": bool(TIMEOUT_STRICT.search(text)),
            "timeout_mention": bool(TIMEOUT_MENTION.search(text)), "infra": infra.group(0) if infra else None,
            "lines": text.count("\n")}


CATEGORIES = ("timeout", "lint/typecheck", "infra/upload", "test", "unknown")


def categorize(job, parsed):
    """(category, evidence) for one failed job, first match wins; 'unknown' when nothing supports a call."""
    parsed = parsed or {}
    if job.get("status") == "timed_out":
        return "timeout", "job conclusion timed_out"
    if parsed.get("timeout"):
        return "timeout", "timeout marker in log"
    if LINT_JOB.search(job["name"]):
        return "lint/typecheck", "job name"
    if parsed.get("tests"):
        return "test", "%d failing test id(s) in log" % len(parsed["tests"])
    if INFRA_JOB.search(job["name"]):
        return "infra/upload", "job name"
    if parsed.get("infra"):
        return "infra/upload", "log: %s" % parsed["infra"]
    return "unknown", "no evidence" if parsed.get("ok") else "log unavailable"


# ------------------------------------------------------------------------------------- detection

def _order(a):
    return (a.get("created_at") or "", a["run_id"], a.get("attempt") or 1)


def detect(attempts):
    """Flaky episodes per (workflow, commit): failed attempt(s) followed by a passed one.

    Attempts are ordered by (run created_at, run id, attempt number). Cancelled/other attempts are
    neither a fail nor a pass. Each pass closes the failures since the previous pass into one episode.
    """
    groups = collections.defaultdict(list)
    for a in attempts:
        if a.get("commit"):
            groups[(a.get("workflow"), a["commit"])].append(a)
    episodes = []
    for (wf, commit), xs in groups.items():
        failed = []
        for a in sorted(xs, key=_order):
            if a["status"] == "failure":
                failed.append(a)
            elif a["status"] == "success":
                if failed:
                    kind = "rerun" if failed[0]["run_id"] == a["run_id"] else "new-run"
                    episodes.append({"workflow": wf, "commit": commit, "kind": kind, "failed": failed, "passed": a})
                failed = []
    return sorted(episodes, key=lambda e: _order(e["passed"]), reverse=True)


# ------------------------------------------------------------------------------------- aggregation

def _job_min(j):
    if j.get("status") in (None, "skipped"):
        return 0.0
    return common.minutes(j.get("started_at"), j.get("finished_at"))


def short_job(name):
    """Matrix suffix dropped: `Run tests (3, 10)` -> `Run tests`."""
    return re.sub(r"\s*\(.*\)$", "", name)


def aggregate(episodes, pr_runs, jobs_fn, log_fn, test_job_regex=DEFAULT_TEST_JOB_REGEX,
              parsers=("pytest", "vitest"), retried_and_passed=None):
    """Report dict. jobs_fn(attempt) -> [job]; log_fn(job) -> parsed dict (analyze_log) or None.

    Logs are read for every failed job of a failed attempt; `test_job_logs_parsed` counts those whose
    name matches test_job_regex.
    """
    test_re = re.compile(test_job_regex)
    failed_attempts = [a for e in episodes for a in e["failed"]]
    jobs = dict(zip([id(a) for a in failed_attempts], _pmap(jobs_fn, failed_attempts)))
    failed_jobs = [j for a in failed_attempts for j in jobs[id(a)] if j.get("status") in ("failure", "timed_out")]
    parsed = dict(zip([j["id"] for j in failed_jobs], _pmap(log_fn, failed_jobs)))

    job_names, tests, files, cats = collections.Counter(), collections.Counter(), collections.Counter(), collections.Counter()
    parse_ok = parse_total = mentions = 0
    extra_job = extra_wait = first_job = first_wait = 0.0
    detail = []
    for e in episodes:
        rec = {"run_id": e["passed"]["run_id"], "sha": e["commit"][:10], "workflow": e["workflow"], "kind": e["kind"],
               "passed_attempt": e["passed"].get("attempt"), "failed_attempts": [[a["run_id"], a.get("attempt")] for a in e["failed"]],
               "failed_jobs": [], "tests": [], "categories": []}
        for k, a in enumerate(e["failed"]):
            jm = sum(_job_min(j) for j in jobs[id(a)])
            wm = common.minutes(a.get("started_at") or a.get("created_at"), a.get("finished_at"))
            extra_job += jm
            extra_wait += wm
            if k == 0:
                first_job += jm
                first_wait += wm
            for j in jobs[id(a)]:
                if j.get("status") not in ("failure", "timed_out"):
                    continue
                p = parsed.get(j["id"]) or {"ok": False}
                name = short_job(j["name"])
                job_names[name] += 1
                rec["failed_jobs"].append(name)
                if test_re.search(j["name"]):
                    parse_total += 1
                    if p.get("ok"):
                        parse_ok += 1
                        mentions += bool(p.get("timeout_mention"))
                for t in p.get("tests", []):
                    tests[t] += 1
                    files[t.split("::")[0].split(" > ")[0]] += 1
                    rec["tests"].append(t)
                cat, why = categorize(j, p)
                cats[cat] += 1
                rec["categories"].append([name, cat, why])
        if not rec["categories"]:  # the attempt failed but no job did (e.g. only cancelled jobs)
            states = collections.Counter(j.get("status") for a in e["failed"] for j in jobs[id(a)])
            cats["unknown"] += 1
            rec["categories"].append([None, "unknown", "no failed job (%s)" % dict(states)])
        detail.append(rec)

    reruns = [e for e in episodes if e["kind"] == "rerun"]
    rerun_runs = {e["passed"]["run_id"] for e in reruns}  # one run can hold several fail->pass episodes
    n = pr_runs
    return {
        "method": "flaky = within one (workflow, commit), a failed attempt followed by a passed attempt with no code change; "
                  "kind rerun = later attempt of the same run, new-run = later separate run; failed test ids parsed from "
                  "job logs (names/counts only, raw logs not kept)",
        "pr_runs": n,
        "retried_and_passed": retried_and_passed,
        "flaky_episodes": len(episodes),
        "flaky_runs": len(rerun_runs),
        "flaky_rerun_episodes": len(reruns),
        "flaky_run_pct_of_pr_runs": round(100.0 * len(rerun_runs) / n, 2) if n else None,
        "flaky_new_run": len(episodes) - len(reruns),
        "extra_attempts": len(failed_attempts),
        "failed_jobs_in_flaky_attempts": dict(job_names.most_common(15)),
        "categories": {c: cats[c] for c in CATEGORIES if cats[c]},
        "test_job_logs_parsed": {"ok": parse_ok, "total": parse_total,
                                 "pct": round(100.0 * parse_ok / parse_total, 1) if parse_total else None},
        "logs_mentioning_timeout": mentions,
        "distinct_flaky_tests": len(tests), "distinct_flaky_files": len(files),
        "top_tests": tests.most_common(20), "top_files": files.most_common(20),
        "cost_estimate": {"extra_job_minutes": round(extra_job), "extra_wait_minutes": round(extra_wait),
                          "first_failed_attempt_only": {"extra_job_minutes": round(first_job), "extra_wait_minutes": round(first_wait)},
                          "label": "estimate: job-minutes of the failed attempts in flaky episodes; wait = those attempts' "
                                   "started->finished time (lost before the passing attempt); first_failed_attempt_only = one failed attempt per episode (lower bound)"},
        "parsers": list(parsers),
        "flaky_runs_detail": detail,
    }


def _pmap(fn, xs):
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
        return list(ex.map(fn, xs))


# ------------------------------------------------------------------------------------- GitHub

def _gh():
    from engine.audit import github  # noqa: PLC0415  (lazy: keeps the pure functions import-light)
    return github


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _get_log(url, headers, retries=4):
    """Job log text. The API answers 302 to a signed storage URL, which is fetched WITHOUT the token."""
    opener = urllib.request.build_opener(_NoRedirect)
    for attempt in range(retries):
        try:
            try:
                with opener.open(urllib.request.Request(url, headers=headers), timeout=60) as r:
                    return r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                if e.code not in (301, 302, 303, 307, 308):
                    raise
                loc = e.headers["Location"]
            with urllib.request.urlopen(urllib.request.Request(loc, headers={"User-Agent": "ci-casestudies-engine"}), timeout=120) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == retries - 1:
                raise common.HTTPStatusError(e.code, url) from e
        except OSError:
            if attempt == retries - 1:
                raise
        time.sleep(5 * (attempt + 1))


def _parser_key(parsers):
    """`pytest-vitest2`: a parser at version 1 keeps its bare name, so older cached results stay valid."""
    return "-".join(p if PARSER_VERSIONS.get(p, 1) == 1 else "%s%d" % (p, PARSER_VERSIONS[p]) for p in parsers)


def github_log_fn(project, cache_dir, parsers):
    gh = _gh()
    d = os.path.join(cache_dir, "flaky_logs")

    def fn(job):
        key = os.path.join(d, "%d.v%d.%s.json" % (job["id"], PARSE_VERSION, _parser_key(parsers)))
        if os.path.exists(key):
            with open(key) as f:
                return json.load(f)
        try:
            res = analyze_log(_get_log("%s/repos/%s/actions/jobs/%d/logs" % (gh.API, project, job["id"]), gh._headers()), parsers)
        except (common.HTTPStatusError, OSError) as e:
            res = {"ok": False, "error": str(e)[:200]}
        os.makedirs(d, exist_ok=True)
        with open(key, "w") as f:
            json.dump(res, f)
        return res
    return fn


def github_jobs_fn(project, cache_dir):
    gh = _gh()

    def fn(a):
        jobs, page = [], 1
        while True:
            data = common.http_get_json("%s/repos/%s/actions/runs/%d/attempts/%d/jobs?per_page=100&page=%d"
                                        % (gh.API, project, a["run_id"], a["attempt"], page), gh._headers(), cache_dir)
            jobs += data["jobs"]
            if len(jobs) >= data["total_count"] or not data["jobs"]:
                break
            page += 1
        return [{"id": j["id"], "name": j["name"], "status": j.get("conclusion"), "started_at": j.get("started_at"),
                 "finished_at": j.get("completed_at")} for j in jobs]
    return fn


def _attempt(r, n, workflow):
    return {"run_id": r["id"], "attempt": n, "workflow": workflow, "commit": r.get("head_sha"),
            "status": r.get("conclusion") or "unknown", "created_at": r["created_at"],
            "started_at": r.get("run_started_at"), "finished_at": r.get("updated_at")}


def github_attempts(project, window, cache_dir, workflows=None, events=None):
    """(PR runs with a success|failure conclusion that ran >= 1 job, all attempt records, zero-job run count).

    Earlier attempts of re-run runs come from /actions/runs/{id}/attempts/{n} (cached); the run listing gives
    the last one. An attempt that concluded failure without running a single job (e.g. a fork PR awaiting
    approval that GitHub closes as failure) never executed: it gets status no_jobs, so it is neither a
    failure in an episode nor, for a latest attempt, a PR run in the denominator (same rule as the audit).
    """
    gh = _gh()
    listed = [r for r in gh._runs(project, window, cache_dir, workflows)
              if r.get("conclusion") in ("success", "failure") and (not events or r.get("event") in events)]
    latest_jobs = _pmap(lambda r: gh._jobs(project, r["id"], cache_dir), listed)  # same cached call as the audit
    zero = {r["id"] for r, js in zip(listed, latest_jobs) if not js}
    runs = [r for r in listed if r["id"] not in zero]
    wf = lambda r: r.get("path") or r.get("name")  # noqa: E731
    earlier = [(r, n) for r in runs for n in range(1, (r.get("run_attempt") or 1))]
    got = _pmap(lambda rn: common.http_get_json("%s/repos/%s/actions/runs/%d/attempts/%d" % (gh.API, project, rn[0]["id"], rn[1]),
                                                gh._headers(), cache_dir), earlier)
    jobs_fn = github_jobs_fn(project, cache_dir)
    failed_earlier = [(r, n) for (r, n), a in zip(earlier, got) if a.get("conclusion") == "failure"]
    empty = {(r["id"], n) for (r, n), js in zip(failed_earlier, _pmap(lambda rn: jobs_fn({"run_id": rn[0]["id"], "attempt": rn[1]}),
                                                                       failed_earlier)) if not js}
    attempts = [_attempt(r, r.get("run_attempt") or 1, wf(r)) for r in runs]
    attempts += [dict(_attempt(a, n, wf(r)), created_at=r["created_at"],
                      **({"status": "no_jobs"} if (r["id"], n) in empty else {})) for (r, n), a in zip(earlier, got)]
    return runs, attempts, len(zero)


def run_github(project, window, cache_dir, workflows=None, test_job_regex=DEFAULT_TEST_JOB_REGEX,
               parsers=("pytest", "vitest"), events=None):
    runs, attempts, zero = github_attempts(project, window, cache_dir, workflows, events)
    print("  %d PR runs (%d zero-job runs excluded), %d attempt records" % (len(runs), zero, len(attempts)), file=sys.stderr)
    episodes = detect(attempts)
    rep = aggregate(episodes, len(runs), github_jobs_fn(project, cache_dir), github_log_fn(project, cache_dir, parsers),
                    test_job_regex=test_job_regex, parsers=parsers,
                    retried_and_passed=sum((r.get("run_attempt") or 1) > 1 and r["conclusion"] == "success" for r in runs))
    out = {"provider": "github", "project": project, "window": window.as_dict(), "workflows": workflows or "all"}
    out.update(rep)
    out["no_job_runs_excluded"] = zero
    out["no_job_attempts"] = sum(a["status"] == "no_jobs" for a in attempts)
    out["sources"] = ["GitHub REST API: /actions/runs (per day, per event), /actions/runs/{id}/attempts/{n}, "
                      "/actions/runs/{id}/attempts/{n}/jobs, /actions/jobs/{id}/logs (parsed, not kept)"]
    return out
