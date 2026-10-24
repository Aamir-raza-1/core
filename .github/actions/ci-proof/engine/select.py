"""Change-aware test selection at test-file level. Every repo-specific rule comes from the profile.

Rules, first match wins per changed file (profile field in brackets):
  run_everything_when glob                -> FULL
  dependency_lockfiles glob               -> tests of the areas whose dependency manifest changed in
                                             the same change [dependency_manifests + area_rule];
                                             no manifest changed -> FULL
  ignore glob                             -> nothing
  tooling_scopes glob                     -> that tool's test folder(s)
  a known test file (co-located or not)   -> itself
  under test_roots:
    runner config file (conftest.py)      -> every test file under its folder
    test file                             -> itself
    other source file (helpers)           -> test files importing it + its folder's tests
    data file                             -> its folder's tests (runner data dirs such as fixtures/ cut off)
                                             + tests that opened it at runtime; neither -> its area's tests
  under source_roots:
    module in the import graph            -> body-only edit with a runtime map: runtime users + area tests
                                             otherwise: static importers (+ runtime users: dynamic loading)
    other file inside an area [area_rule(s)], or a statically imported data file
                                          -> the area's tests + runtime users + static importers
    anything else                         -> FULL (conservative)
  anything else                           -> FULL (conservative)
Unless FULL, tests matching `always_run` globs are added (tests whose inputs neither map can see,
e.g. ones that read repo files from disk).

Inputs: the static import map {test_file: [repo files]} (engine/static_imports.py), optionally the
runtime map from the traced run and real seconds per test file.
"""
import collections
import gzip
import json
import os
import re

# Test-runner conventions (not repo rules): how to recognise test files, folder config and data dirs.
RUNNERS = {
    "pytest": {"test_file": re.compile(r"(^|/)test_[^/]*\.py$"), "folder_config": "conftest.py",
               "code_ext": ".py", "data_dirs": ["fixtures", "snapshots", "__snapshots__"]},
    "vitest": {"test_file": re.compile(r"\.(spec|test)\.[cm]?[jt]sx?$"), "folder_config": None,
               "code_ext": (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"),
               "data_dirs": ["__fixtures__", "__snapshots__", "fixtures"]},
    "jest": {"test_file": re.compile(r"\.(spec|test)\.[cm]?[jt]sx?$"), "folder_config": None,
             "code_ext": (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"),
             "data_dirs": ["__fixtures__", "__snapshots__", "fixtures"]},
    # Go: the test unit is a package directory (`go test` runs per package); the static map is
    # {package dir: [package dirs in its test binary's closure]} (experiments/k8s/build_go_map.py).
    "go": {"test_file": re.compile(r"_test\.go$"), "folder_config": None, "code_ext": ".go",
           "data_dirs": ["testdata"], "unit": "dir"},
}


def unit_key(f, run, idx):
    """The key a changed file is looked up by: itself, or for package-level runners (Go) its package dir.
    .go files -> their directory; other files -> the nearest enclosing directory the map knows (embedded or
    read files belong to that package); none -> the file itself."""
    if run.get("unit") != "dir":
        return f
    d = os.path.dirname(f)
    if f.endswith(run["code_ext"]):
        return d
    while d and d not in idx.importers and d not in idx.test_set:
        d = os.path.dirname(d)
    return d or f


def glob_regex(pattern):
    """Path glob -> regex: `**/` any dirs, `/**` everything below, `*` within a segment, `{name}` captures one segment."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("/**", i) and i + 3 == len(pattern):
            out.append("(?:/.*)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern.startswith("{name}", i):
            out.append("(?P<name>[^/]+)")
            i += 6
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


class Globs:
    def __init__(self, patterns):
        self.res = [glob_regex(p) for p in patterns or []]

    def __call__(self, path):
        return any(r.match(path) for r in self.res)


class AreaRule:
    """`src/{name}/** -> tests/{name}/` (one rule or several, first match wins): the test folder
    of the area a source file belongs to. Areas are returned as (rule index, name)."""

    def __init__(self, rules):
        rules = [rules] if isinstance(rules, str) else list(rules or [])
        self.rules = []
        for rule in rules:
            src, dst = [x.strip() for x in rule.split("->")]
            self.rules.append((glob_regex(src), dst))

    def name(self, path):
        for i, (rx, _) in enumerate(self.rules):
            m = rx.match(path)
            if m:
                return (i, m.group("name"))
        return None

    def tests_dir(self, area):
        i, name = area
        return self.rules[i][1].replace("{name}", name)


class Rules:
    def __init__(self, profile):
        self.runner = RUNNERS[profile["test_runner"]]
        self.full = Globs(profile["run_everything_when"])
        self.lockfiles = Globs(profile.get("dependency_lockfiles"))
        self.manifests = Globs(profile.get("dependency_manifests"))
        self.ignore = Globs(profile.get("ignore"))
        self.tooling = [(glob_regex(k), [v] if isinstance(v, str) else list(v))
                        for k, v in (profile.get("tooling_scopes") or {}).items()]
        self.area = AreaRule(profile.get("area_rules") or profile.get("area_rule"))
        self.always_run = Globs(profile.get("always_run"))
        self.test_roots = tuple(r.rstrip("/") + "/" for r in profile["test_roots"])
        self.source_roots = tuple(r.rstrip("/") + "/" for r in profile["source_roots"])


class TestIndex:
    """All test files, who imports what (static), and who used what at runtime (traced run)."""

    def __init__(self, static, runtime=None, seconds=None):
        self.test_files = sorted(static)
        self.test_set = set(self.test_files)
        self.importers = collections.defaultdict(set)
        for t, deps in static.items():
            for d in deps:
                self.importers[d].add(t)
        self.runtime = runtime
        self.seconds = seconds
        self._folders = {}

    def folder(self, prefix):
        if prefix not in self._folders:
            self._folders[prefix] = {t for t in self.test_files if t.startswith(prefix)}
        return self._folders[prefix]

    def used_at_runtime(self, f):
        return self.runtime.get(f, set()) if self.runtime is not None else set()

    def share(self, selected):
        if self.seconds:
            total = sum(self.seconds.get(t, 0) for t in self.test_files)
            return round(100.0 * sum(self.seconds.get(t, 0) for t in selected) / total, 1)
        return round(100.0 * len(selected) / len(self.test_files), 1)


def select(files, kinds, rules, idx, trace=None):
    """files: changed paths; kinds: {path: 'body-only'|'module-level'|...}. -> (selected, full, reasons).

    trace: optional list; each rule that fires appends (changed file, rule, tests it selects or None for
    the full suite, unknown) where unknown=True marks the conservative fallbacks (the rules cannot tell)."""
    run = rules.runner
    selected, full, reasons = set(), False, collections.Counter()

    def note(f, rule, found, unknown=False):
        if trace is not None:
            trace.append((f, rule, found, unknown))
    areas_with_manifest = {rules.area.name(f) for f in files if rules.manifests(f)} - {None}
    for f in files:
        k = unit_key(f, run, idx)
        tooling = next((dirs for rx, dirs in rules.tooling if rx.match(f)), None)
        if rules.full(f):
            full = True
            reasons["full: " + f] += 1
            note(f, "run_everything_when", None)
        elif rules.lockfiles(f):
            if not areas_with_manifest:
                full = True
                reasons["full: lockfile without manifest"] += 1
                note(f, "dependency_lockfiles: no dependency manifest changed", None)
            found = set()
            for a in areas_with_manifest:
                found |= idx.folder(rules.area.tests_dir(a))
            selected |= found
            if areas_with_manifest:
                note(f, "dependency_lockfiles: tests of the areas whose manifest changed", found)
            reasons["deps"] += 1
        elif rules.ignore(f):
            reasons["no tests"] += 1
            note(f, "ignore", set())
        elif tooling is not None:
            found = set()
            for d in tooling:
                found |= idx.folder(d)
            selected |= found
            reasons["tooling"] += 1
            note(f, "tooling_scopes", found)
        elif k in idx.test_set and (k == f or run["test_file"].search(f)) or (
                (k != f or not f.startswith(rules.test_roots)) and run["test_file"].search(f)
                and not any("/%s/" % dd in "/" + f for dd in run["data_dirs"])):
            # package-level runners: a _test.go file selects its package (even one the map lacks: new tests);
            # file-level runners: a test file the map lacks outside test_roots (new co-located test) selects itself
            selected.add(k)
            reasons["test file"] += 1
            note(f, "test file (itself)" if k == f else "test file (its package)", {k})
        elif f.startswith(rules.test_roots):
            d = os.path.dirname(f)
            if os.path.basename(f) == run["folder_config"]:
                found, rule = idx.folder(d + "/"), "test_roots: runner config (its folder's tests)"
            elif run["test_file"].search(f):
                found, rule = {f}, "test_roots: test file (itself)"
            elif f.endswith(run["code_ext"]):
                found, rule = idx.importers.get(f, set()) | idx.folder(d + "/"), "test_roots: helper (importers + its folder's tests)"
            else:
                for dd in run["data_dirs"]:
                    d = d.split("/" + dd)[0]
                found = idx.folder(d + "/") | idx.used_at_runtime(f)  # + tests that opened it (traced run)
                rule = "test_roots: data file (its folder's tests + runtime readers)"
                if not found:  # nobody visibly reads it (e.g. read at collection time): the area's tests
                    area = rules.area.name(f)
                    found = idx.folder(rules.area.tests_dir(area)) if area else set()
                    reasons["data file, no reader seen: area"] += 1
                    rule = "test_roots: data file, no reader seen (area tests)"
            selected |= found
            note(f, rule, found, unknown=rule.endswith("(area tests)"))
            reasons["tests"] += 1
        elif f.startswith(rules.source_roots):
            area = rules.area.name(f)
            area_tests = idx.folder(rules.area.tests_dir(area)) if area else set()
            data_dir = next((dd for dd in run["data_dirs"] if "/%s/" % dd in "/" + f), None) if k != f else None
            if data_dir:  # Go testdata/: read by the owning package's tests (and its subpackages via ../testdata)
                owner = ("/" + f).split("/%s/" % data_dir)[0].lstrip("/")
                found = {t for t in idx.test_files if t == owner or t.startswith(owner + "/")}
                selected |= found
                reasons["test data"] += 1
                note(f, "source_roots: test data (owning package + packages below it)", found)
            elif f.endswith(run["code_ext"]) and k != f and k not in idx.importers:
                # a package no test binary depends on at the map's commit (new, or main-only/other-OS code):
                # its own directory's units (new tests show up as _test.go files); flagged as unknown
                found = {t for t in idx.test_files if t == k or t.startswith(k + "/")}
                selected |= found
                reasons["code outside the map"] += 1
                note(f, "source_roots: package outside the map (its own directory's tests)", found, unknown=True)
            elif f.endswith(run["code_ext"]) and k in idx.importers:
                if kinds.get(f) == "body-only" and idx.runtime is not None:
                    found = idx.used_at_runtime(f) | area_tests
                    reasons["code body (runtime)"] += 1
                    note(f, "source_roots: function-body edit (runtime users + area tests)", found)
                else:
                    found = idx.importers[k] | idx.used_at_runtime(f)
                    reasons["code module (static%s)" % ("+runtime" if idx.runtime is not None else "")] += 1
                    note(f, "source_roots: module-level edit (static importers + runtime users)", found)
                selected |= found
            elif area or k in idx.importers:
                # non-code file (data, svg, css…): area tests + runtime users + static importers if imported
                found = area_tests | idx.used_at_runtime(f) | idx.importers.get(k, set())
                selected |= found
                reasons["area folder"] += 1
                note(f, "source_roots: non-code file (area tests + runtime users + static importers)", found)
            else:
                full = True
                reasons["full: " + f] += 1
                note(f, "unknown: source file outside every area and the import graph (full run)", None, unknown=True)
        else:
            full = True
            reasons["full: " + f] += 1
            note(f, "unknown: file outside source_roots/test_roots and every rule (full run)", None, unknown=True)
    if not full and rules.always_run.res:
        extra = {t for t in idx.test_files if rules.always_run(t)}
        if extra - selected:
            reasons["always run"] += 1
        selected |= extra
        note(None, "always_run", extra)
    return selected, full, reasons


DECISIONS_SCHEMA = "engine.decisions/v1"
DECISIONS = ("MUST_RUN", "SAFE_TO_SKIP", "UNCERTAIN")


def explain(files, kinds, rules, idx, map_source=None, map_commit=None, cap=20):
    """Typed decision per test file (D10): MUST_RUN | SAFE_TO_SKIP | UNCERTAIN + reason + proof.

    UNCERTAIN = only the conservative fallbacks fired for this test (rules say "unknown"); it resolves to
    MUST_RUN, so `selected` (MUST_RUN + UNCERTAIN) is exactly what `select` returns."""
    trace = []
    selected, full, reasons = select(files, kinds, rules, idx, trace)
    footprint = collections.defaultdict(set)  # test file -> changed files it depends on (static or runtime)
    for f in files:
        for t in idx.importers.get(unit_key(f, rules.runner, idx), ()):
            footprint[t].add(f)
        for t in idx.used_at_runtime(f):
            footprint[t].add(f)
        if f in idx.test_set:
            footprint[f].add(f)
    hits = {False: collections.defaultdict(list), True: collections.defaultdict(list)}  # unknown -> test -> [(f, rule)]
    whole = {False: [], True: []}
    for f, rule, found, unknown in trace:
        if found is None:
            whole[unknown].append((f, rule))
        else:
            for t in found:
                hits[unknown][t].append((f, rule))
    universe = idx.test_files if full else sorted(set(idx.test_files) | selected)

    def why(pairs):
        f, rule = sorted(pairs, key=lambda p: (p[0] is None, p[0] or ""))[0]
        more = len(pairs) - 1
        return "%s%s%s" % ("%s: " % f if f else "", rule, " (+%d more)" % more if more else "")
    decisions, counts = {}, collections.Counter()
    for t in universe:
        if t in hits[False] or whole[False]:
            pairs = hits[False].get(t) or whole[False]
            d = "MUST_RUN"
        elif t in hits[True] or whole[True]:
            pairs = hits[True].get(t) or whole[True]
            d = "UNCERTAIN"
        else:
            pairs, d = [], "SAFE_TO_SKIP"
        fp = sorted(footprint.get(t, ()))
        trig = sorted({f for f, _ in pairs if f})
        decisions[t] = {
            "decision": d,
            "reason": why(pairs) if pairs else "no rule selects it: none of the %d changed files is in its import closure or runtime footprint" % len(files),
            "proof": {"diff_files": trig[:cap], "diff_files_total": len(trig), "matched_footprint": fp[:cap],
                      "matched_footprint_total": len(fp), "map_source": map_source, "map_commit": map_commit},
        }
        counts[d] += 1
    chosen = sorted(t for t, v in decisions.items() if v["decision"] != "SAFE_TO_SKIP")
    want = list(idx.test_files) if full else sorted(selected)
    if chosen != want:  # invariant: typed decisions never change the selection
        raise AssertionError("explain: decisions disagree with select (%d vs %d)" % (len(chosen), len(want)))
    return {"schema": DECISIONS_SCHEMA, "diff_files": list(files), "full": full, "reasons": dict(reasons),
            "map_source": map_source, "map_commit": map_commit, "counts": {k: counts[k] for k in DECISIONS},
            "selected": chosen, "decisions": decisions}


def load_runtime_map(path):
    """{repo file: {test files}} from the analyzer's map (test-file .json.gz, or per-test .json)."""
    runtime = collections.defaultdict(set)
    if path.endswith(".gz"):
        with gzip.open(path, "rt") as f:
            m = json.load(f)
        for key, ids in m["file_to_test_files"].items():
            if key.startswith("repo:"):
                runtime[key[5:]] = {m["test_files"][i] for i in ids}
    else:
        with open(path) as f:
            m = json.load(f)
        for key, ids in m["file_to_tests"].items():
            if key.startswith("repo:"):
                for i in ids:
                    runtime[key[5:]].add(m["tests"][i].split("::")[0])
    return runtime


def replay(prs, rules, idx):
    """Selection for each recorded change; savings weighted by each change's full-run test minutes."""
    rows = []
    for p in prs:
        selected, full, reasons = select(p["files"], p.get("change_kinds", {}), rules, idx)
        pct = 100.0 if full else idx.share(selected)
        rows.append({"pr": p["pr"], "label": p.get("label"), "minutes": p["minutes"], "pct_test_files": pct,
                     "selected": None if full else len(selected), "reasons": dict(reasons)})
    total = sum(r["minutes"] for r in rows)
    ours = sum(r["minutes"] * r["pct_test_files"] / 100 for r in rows)
    summary = {
        "prs": len(rows),
        "test_files_total": len(idx.test_files),
        "runtime_map": bool(idx.runtime),
        "full_run_test_minutes_today": round(total),
        "estimated_test_minutes_with_selection": round(ours),
        "estimated_saving_pct": round(100 * (1 - ours / total), 1) if total else None,
        "prs_still_full": sum(1 for r in rows if r["pct_test_files"] == 100.0),
        "prs_under_10pct": sum(1 for r in rows if r["pct_test_files"] < 10),
        "median_pct_test_files": sorted(r["pct_test_files"] for r in rows)[len(rows) // 2] if rows else None,
        "note": "Weighted by real seconds per test file." if idx.seconds else
                "Test-file granularity; minutes assume run time is proportional to the share of test files.",
    }
    return {"summary": summary, "prs": rows}
