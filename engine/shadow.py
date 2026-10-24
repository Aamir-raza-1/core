"""Passive shadow (real-failure safety): would our selection have run the tests that really failed in CI?

  python3 -m engine.cli shadow --provider github --project owner/repo --days 90 --end YYYY-MM-DD \
      --workflow ci.yml --test-job-regex '^test' --parser pytest --profile P --index repo.idx --out shadow.json

For each completed CI run (attempt) in a window:
  (a) the tested commit's changed files vs its base (exact SHAs; never the PR's current file list: heads move)
  (b) our selection for those files (engine.select on the compiled index or the maps)
  (c) the test ids that failed in that run (job logs via engine.flaky parsers, or junit)
  (d) each failing test, classified:
        caught   its test file is in our selection (or the change runs the full suite)
        missed   outside our selection = a real-world miss (each one needs a diagnosis)
        flaky    the same commit later passed in the same workflow (engine.flaky.detect rule: fail-then-pass);
                 reported separately, never counted as caught or missed
      and each failing run without any test id is setup-only (install, services, collection crash, timeout).

The source is pluggable: `run(records, select_fn, changed_fn)` takes any iterable of records
  {run_id, base_sha, head_sha, failed_test_ids, status}   (status: success | failure | other)
with optional keys attempt, created_at, workflow, pr, changed_files (when the source already knows the diff),
failed_jobs, failed_jobs_without_tests, categories. `github_records` is the GitHub Actions source; a Prow/GCS
source only has to yield the same records. No repo literals here: repo rules come from the profile.
"""
import collections
import sys

from engine import flaky

SCHEMA = "engine.shadow/v1"
MAX_COMPARE_PAGES = 30  # GitHub compare lists at most 3,000 files (100 per page)


def test_file(test_id):
    """`path/test_x.py::Class::test[param]` -> `path/test_x.py`."""
    return test_id.split("::", 1)[0]


class PathResolver:
    """Test-id path -> repo path. pytest prints node ids relative to its rootdir, which is a subfolder when the
    tests run from there (a nested pytest.ini): `djangoapps/x/tests/test_y.py` for `cms/djangoapps/...`. A path
    the index doesn't know is resolved to the unique known test file ending in `/<path>`; ambiguous or unknown
    paths stay as they are (then they can only be caught by a full run, never silently matched)."""

    def __init__(self, test_files):
        self.known = set(test_files or ())
        self.by_base = collections.defaultdict(list)
        for t in self.known:
            self.by_base[t.rsplit("/", 1)[-1]].append(t)

    def __call__(self, path):
        if not self.known or path in self.known:
            return path
        hits = [t for t in self.by_base.get(path.rsplit("/", 1)[-1], ()) if t.endswith("/" + path)]
        return hits[0] if len(hits) == 1 else path


def flaky_heads(records):
    """{(workflow, head_sha)} where a failed attempt was followed by a passed one (engine.flaky.detect)."""
    attempts = [{"run_id": r["run_id"], "attempt": r.get("attempt") or 1, "workflow": r.get("workflow"),
                 "commit": r.get("head_sha"), "status": r.get("status"), "created_at": r.get("created_at") or ""}
                for r in records]
    return {(e["workflow"], e["commit"]) for e in flaky.detect(attempts)}


def classify(rec, selected, full, flaky_commit, test_files=None, resolve=None):
    """Per failing test id -> caught | missed | flaky, with the evidence used."""
    rows = []
    resolve = resolve or PathResolver(test_files)
    for t in sorted(set(rec.get("failed_test_ids") or ())):
        f = resolve(test_file(t))
        inside = full or f in selected
        if flaky_commit:
            verdict = "flaky"
        else:
            verdict = "caught" if inside else "missed"
        rows.append({"test": t, "file": f, "verdict": verdict, "in_selection": inside,
                     "known_test_file": None if test_files is None else f in test_files})
    return rows


def run(records, select_fn, changed_fn=None, test_files=None, meta=None):
    """records: iterable of run records (see module doc). select_fn(files) -> (selected set, full bool, reasons).
    changed_fn(record) -> (files list or None, base_sha) for records without `changed_files`; called only for
    failing runs with test ids (the only ones that can be caught or missed)."""
    records = list(records)
    resolve = PathResolver(test_files)
    flaky_set = flaky_heads(records)
    out_runs, counts = [], collections.Counter()
    repeat = collections.Counter()  # test id -> distinct heads it failed on (shared breakage / flaky hint)
    for r in records:
        if r.get("status") == "failure" and not ((r.get("workflow"), r.get("head_sha")) in flaky_set):
            for t in set(r.get("failed_test_ids") or ()):
                repeat[(t, r.get("head_sha"))] += 1
    heads_per_test = collections.Counter(t for (t, _h) in repeat)
    for r in sorted(records, key=lambda r: (r.get("created_at") or "", r["run_id"], r.get("attempt") or 1)):
        counts["runs"] += 1
        st = r.get("status")
        row = {k: r.get(k) for k in ("run_id", "attempt", "pr", "created_at", "head_sha", "base_sha", "status")}
        if st != "failure":
            counts["runs_" + (st or "unknown")] += 1
            continue  # passing runs carry no failure evidence; kept out of the per-run list (summary counts them)
        counts["failing_runs"] += 1
        ids = r.get("failed_test_ids") or []
        fl = (r.get("workflow"), r.get("head_sha")) in flaky_set
        row.update({"flaky_commit": fl, "failed_jobs": r.get("failed_jobs"),
                    "failed_jobs_without_tests": r.get("failed_jobs_without_tests"), "categories": r.get("categories")})
        if not ids:
            counts["setup_only_runs"] += 1
            row["kind"] = "setup-only"
            out_runs.append(row)
            continue
        counts["failing_runs_with_test_ids"] += 1
        files = r.get("changed_files")
        if files is None and changed_fn is not None:
            files, base = changed_fn(r)
            row["base_sha"] = base or row["base_sha"]
        if files is None:
            counts["runs_without_diff"] += 1
            row.update(kind="no-diff", tests=[{"test": t, "file": test_file(t), "verdict": "unknown"} for t in sorted(set(ids))])
            out_runs.append(row)
            continue
        selected, full, reasons = select_fn(files)
        tests = classify(r, selected, full, fl, test_files, resolve)
        for t in tests:
            t["failed_on_other_heads"] = heads_per_test[t["test"]] - (0 if fl else 1)
            counts["tests_" + t["verdict"]] += 1
        row.update(kind="flaky" if fl else "test-failure", changed_files=len(files), full=full,
                   selected=None if full else len(selected), reasons=dict(reasons), tests=tests,
                   changed=sorted(files)[:50])
        if fl:
            counts["flaky_runs"] += 1
        elif any(t["verdict"] == "missed" for t in tests):
            counts["runs_with_miss"] += 1
        out_runs.append(row)
    caught, missed = counts["tests_caught"], counts["tests_missed"]
    summary = {
        "runs": counts["runs"], "runs_success": counts["runs_success"], "failing_runs": counts["failing_runs"],
        "failing_runs_with_test_ids": counts["failing_runs_with_test_ids"], "setup_only_runs": counts["setup_only_runs"],
        "flaky_runs": counts["flaky_runs"], "runs_without_diff": counts["runs_without_diff"],
        "runs_with_miss": counts["runs_with_miss"],
        "failing_tests_caught": caught, "failing_tests_missed": missed, "failing_tests_flaky": counts["tests_flaky"],
        "recall_pct": round(100.0 * caught / (caught + missed), 1) if caught + missed else None,
        "unit": "a failing test = one (run attempt, test id) pair; flaky = failed on a commit that later passed "
                "in the same workflow (engine.flaky.detect), excluded from caught/missed",
    }
    misses = [{"run_id": x["run_id"], "attempt": x.get("attempt"), "pr": x.get("pr"), "head_sha": x["head_sha"],
               "base_sha": x.get("base_sha"), "test": t["test"], "changed": x.get("changed"),
               "failed_on_other_heads": t["failed_on_other_heads"], "known_test_file": t.get("known_test_file")}
              for x in out_runs if x.get("kind") == "test-failure" for t in x["tests"] if t["verdict"] == "missed"]
    return {"schema": SCHEMA, **(meta or {}), "summary": summary, "misses": misses, "runs": out_runs}


# ------------------------------------------------------------------------------------- GitHub source

def github_records(project, window, cache_dir, workflows=None, events=None, test_job_regex=flaky.DEFAULT_TEST_JOB_REGEX,
                   parsers=("pytest",)):
    """One record per attempt of each completed PR run (engine.flaky.github_attempts: zero-job runs excluded).
    Failing test ids come from the failed test jobs' logs (parsed, cached; raw logs never kept)."""
    import re  # noqa: PLC0415
    rx = re.compile(test_job_regex)
    runs, attempts, _zero = flaky.github_attempts(project, window, cache_dir, workflows, events)
    by_id = {r["id"]: r for r in runs}
    jobs_fn, log_fn = flaky.github_jobs_fn(project, cache_dir), flaky.github_log_fn(project, cache_dir, parsers)

    def one(a):
        r = by_id[a["run_id"]]
        prs = r.get("pull_requests") or []
        rec = {"run_id": a["run_id"], "attempt": a["attempt"], "workflow": a["workflow"], "created_at": a["created_at"],
               "head_sha": a["commit"], "base_sha": None, "status": a["status"], "failed_test_ids": [],
               "pr": prs[0]["number"] if prs else None,
               "_base_ref": prs[0]["base"]["ref"] if prs else None,
               "_head": ((r.get("head_repository") or {}).get("owner") or {}).get("login"), "_branch": r.get("head_branch")}
        if a["status"] != "failure":
            return rec
        failed = [j for j in jobs_fn(a) if j["status"] in ("failure", "timed_out") and rx.search(j["name"])]
        ids, without, cats = set(), 0, collections.Counter()
        for j in failed:
            parsed = log_fn(j)
            cats[flaky.categorize(j, parsed)[0]] += 1
            if parsed.get("tests"):
                ids.update(parsed["tests"])
            else:
                without += 1
        rec.update(failed_test_ids=sorted(ids), failed_jobs=len(failed), failed_jobs_without_tests=without, categories=dict(cats))
        return rec
    recs = flaky._pmap(one, attempts)
    print("  shadow: %d attempt records, %d failing" % (len(recs), sum(r["status"] == "failure" for r in recs)), file=sys.stderr)
    return recs


def github_changed_fn(project, cache_dir, default_base="master"):
    """record -> (changed files, merge-base sha) from the compare API: `<base tip at run time>...<tested head sha>`.

    pull_request runs test the PR head merged into its base branch's tip of that moment; the three-dot compare
    lists exactly the PR's changes from their merge base (commits API `until=<run created_at>` gives that tip).
    Fork PRs carry no pull_requests in the run payload: the base ref comes from the pulls API by
    `owner:branch`, falling back to `default_base`. Renames list both paths. > 3,000 files -> None (unknown)."""
    from engine.audit import common, github as gh  # noqa: PLC0415
    base_cache = {}

    def base_ref(rec):
        if rec.get("_base_ref"):
            return rec["_base_ref"]
        key = (rec.get("_head"), rec.get("_branch"))
        if key not in base_cache:
            ref = default_base
            if all(key):
                try:
                    pulls = common.http_get_json("%s/repos/%s/pulls?state=all&head=%s:%s&per_page=10"
                                                 % (gh.API, project, key[0], key[1]), gh._headers(), cache_dir)
                    if pulls:
                        ref = pulls[0]["base"]["ref"]
                        rec["pr"] = rec.get("pr") or pulls[0]["number"]
                except common.HTTPStatusError:
                    pass
            base_cache[key] = ref
        return base_cache[key]

    def tip_at(ref, when):
        """The base branch's tip when the run was created: the run tested head merged into that tip. Comparing with
        today's tip breaks once the PR's own commits land in the base (merge commit): the diff would be empty."""
        if not when:
            return ref
        try:
            got = common.http_get_json("%s/repos/%s/commits?sha=%s&until=%s&per_page=1" % (gh.API, project, ref, when),
                                       gh._headers(), cache_dir)
            return got[0]["sha"] if got else ref
        except common.HTTPStatusError:
            return ref

    def compare(base, head):
        files, mb = [], None
        for page in range(1, MAX_COMPARE_PAGES + 1):
            try:
                d = common.http_get_json("%s/repos/%s/compare/%s...%s?per_page=100&page=%d"
                                         % (gh.API, project, base, head, page), gh._headers(), cache_dir)
            except common.HTTPStatusError:
                return None, None
            mb = (d.get("merge_base_commit") or {}).get("sha")
            for f in d.get("files") or []:
                files.append(f["filename"])
                if f.get("previous_filename"):
                    files.append(f["previous_filename"])
            if len(d.get("files") or []) < 100:
                return sorted(set(files)), mb
        return None, mb

    def pr_start(rec):
        """Parent of the PR's first commit (the PR's own start point), for heads already inside the base branch
        (rebase / fast-forward merges keep the SHAs, so a compare with the base tip comes back empty)."""
        try:
            prs = common.http_get_json("%s/repos/%s/commits/%s/pulls" % (gh.API, project, rec["head_sha"]), gh._headers(), cache_dir)
            n = rec.get("pr") or (prs[0]["number"] if prs else None)
            if not n:
                return None
            commits = common.http_get_json("%s/repos/%s/pulls/%d/commits?per_page=100" % (gh.API, project, n), gh._headers(), cache_dir)
        except common.HTTPStatusError:
            return None
        shas = [c["sha"] for c in commits]
        if rec["head_sha"] not in shas or not commits[0].get("parents"):
            return None
        rec["pr"] = n
        return commits[0]["parents"][0]["sha"]

    def fn(rec):
        files, base = compare(tip_at(base_ref(rec), rec.get("created_at")), rec["head_sha"])
        if (files is None or not files) and (base is None or base == rec["head_sha"]):
            start = pr_start(rec)
            if start:
                files, base = compare(start, rec["head_sha"])
        return files, base
    return fn


def _github_source(project, window, cache_dir, workflows=None, events=None, test_job_regex=flaky.DEFAULT_TEST_JOB_REGEX,
                   parsers=("pytest",), **_):
    return (github_records(project, window, cache_dir, workflows, events, test_job_regex, parsers),
            github_changed_fn(project, cache_dir))


# provider -> fn(project, window, cache_dir, **opts) -> (records, changed_fn or None). Add a provider here (e.g. prow).
def _prow_source(project, window, cache_dir, **opts):
    from engine.audit import prow  # noqa: PLC0415
    return prow.shadow_source(project, window, cache_dir, **opts)


SOURCES = {"github": _github_source, "prow": _prow_source}


def select_fn_for(sel, rules, idx):
    """Our selection for a list of changed files: every edit treated as module-level (no body-only shortcut:
    the shadow judges the conservative selection)."""
    return lambda files: sel.select(files, {}, rules, idx)
