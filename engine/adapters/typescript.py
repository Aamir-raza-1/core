"""TypeScript / JavaScript language adapter.

Extracts static import dependency closures (supporting tsconfig paths, barrel files,
and monorepo package resolution) and parses per-test Vitest V8 coverage artifacts.
"""
import glob
import json
import os
import re
from engine.select import RUNNERS

# Regexes for ES module imports/exports, dynamic imports, and CommonJS require()
IMPORT_RE = re.compile(
    r"""(?:import|export)\s+(?:(?:[\w*\s{},$]+from\s+)|(?:\(\s*))?['"]([^'"]+)['"]""",
    re.MULTILINE,
)
REQUIRE_RE = re.compile(
    r"""(?:require|import)\s*\(\s*['"]([^'"]+)['"]\s*\)""",
    re.MULTILINE,
)

TS_EXTENSIONS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")


def strip_json_comments(text):
    """Strip // comments and trailing commas from tsconfig.json."""
    text = re.sub(r"//.*?\n", "\n", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    return text


def load_tsconfig_paths(repo_root):
    """Scan repo for tsconfig.json files and extract compilerOptions.paths mappings."""
    paths_map = {}
    for dirpath, _, filenames in os.walk(repo_root):
        if "node_modules" in dirpath or ".git" in dirpath:
            continue
        for fn in filenames:
            if fn in ("tsconfig.json", "tsconfig.base.json"):
                full_path = os.path.join(dirpath, fn)
                try:
                    with open(full_path, "r", encoding="utf-8") as f:
                        data = json.loads(strip_json_comments(f.read()))
                        opts = data.get("compilerOptions", {})
                        paths = opts.get("paths", {})
                        base_url = opts.get("baseUrl", ".")
                        base_dir = os.path.relpath(os.path.normpath(os.path.join(dirpath, base_url)), repo_root)
                        for alias, targets in paths.items():
                            paths_map[alias] = (base_dir, targets)
                except Exception:
                    continue
    return paths_map


def load_package_manifests(repo_root):
    """Map package.json name to package directory for workspace resolutions."""
    pkg_map = {}
    for dirpath, _, filenames in os.walk(repo_root):
        if "node_modules" in dirpath or ".git" in dirpath:
            continue
        if "package.json" in filenames:
            try:
                with open(os.path.join(dirpath, "package.json"), "r", encoding="utf-8") as f:
                    data = json.load(f)
                    name = data.get("name")
                    if name:
                        rel = os.path.relpath(dirpath, repo_root)
                        pkg_map[name] = rel
            except Exception:
                continue
    return pkg_map


def resolve_specifier(specifier, from_file_rel, repo_root, tsconfig_paths, pkg_map):
    """Resolve an import specifier to a repo-relative file path."""
    from_dir = os.path.dirname(from_file_rel)

    # 1. Relative import (./ or ../)
    if specifier.startswith("./") or specifier.startswith("../"):
        target_base = os.path.normpath(os.path.join(from_dir, specifier))
        resolved = check_file_or_barrel(target_base, repo_root)
        if resolved:
            return resolved

    # 2. tsconfig path aliases (e.g. @/* or ~/ui/*)
    for alias_pattern, (base_dir, targets) in tsconfig_paths.items():
        if alias_pattern.endswith("/*"):
            prefix = alias_pattern[:-2]
            if specifier.startswith(prefix + "/"):
                remainder = specifier[len(prefix) + 1:]
                for target in targets:
                    target_pattern = target.replace("/*", "/" + remainder) if "/*" in target else target
                    cand = os.path.normpath(os.path.join(base_dir, target_pattern))
                    resolved = check_file_or_barrel(cand, repo_root)
                    if resolved:
                        return resolved
        elif alias_pattern == specifier:
            for target in targets:
                cand = os.path.normpath(os.path.join(base_dir, target))
                resolved = check_file_or_barrel(cand, repo_root)
                if resolved:
                    return resolved

    # 3. Monorepo workspace package (e.g. @scope/pkg)
    for pkg_name, pkg_rel in pkg_map.items():
        if specifier == pkg_name:
            resolved = check_file_or_barrel(os.path.join(pkg_rel, "src", "index"), repo_root)
            if resolved:
                return resolved
            resolved = check_file_or_barrel(os.path.join(pkg_rel, "index"), repo_root)
            if resolved:
                return resolved
        elif specifier.startswith(pkg_name + "/"):
            sub = specifier[len(pkg_name) + 1:]
            resolved = check_file_or_barrel(os.path.join(pkg_rel, "src", sub), repo_root)
            if resolved:
                return resolved
            resolved = check_file_or_barrel(os.path.join(pkg_rel, sub), repo_root)
            if resolved:
                return resolved

    return None


def check_file_or_barrel(rel_candidate, repo_root):
    """Check candidate path directly, with TS extensions, or as a barrel directory (index.ts)."""
    full = os.path.join(repo_root, rel_candidate)
    if os.path.isfile(full):
        return rel_candidate

    for ext in TS_EXTENSIONS:
        if os.path.isfile(full + ext):
            return rel_candidate + ext

    # Barrel file: candidate is a directory containing index.ts/js
    for ext in TS_EXTENSIONS:
        barrel = os.path.join(full, "index" + ext)
        if os.path.isfile(barrel):
            return os.path.join(rel_candidate, "index" + ext)

    return None


def extract_imports(file_rel, repo_root, tsconfig_paths, pkg_map):
    """Extract direct repo-relative imports from a TS/JS file."""
    full_path = os.path.join(repo_root, file_rel)
    try:
        with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        return set()

    specifiers = set(IMPORT_RE.findall(content)) | set(REQUIRE_RE.findall(content))
    imported_files = set()
    for s in specifiers:
        resolved = resolve_specifier(s, file_rel, repo_root, tsconfig_paths, pkg_map)
        if resolved and resolved != file_rel:
            imported_files.add(resolved)
    return imported_files


class Adapter:
    def static_deps(self, repo, profile):
        runner_info = RUNNERS.get(profile.get("test_runner", "vitest"), RUNNERS["vitest"])
        test_regex = runner_info["test_file"]

        tsconfig_paths = load_tsconfig_paths(repo)
        pkg_map = load_package_manifests(repo)

        source_roots = profile.get("source_roots", [])
        test_roots = profile.get("test_roots", source_roots)

        # 1. Discover all test and source files
        all_files = []
        test_files = []
        roots = sorted(set(source_roots + test_roots))
        for root in roots:
            for dirpath, _, filenames in os.walk(os.path.join(repo, root)):
                if "node_modules" in dirpath or ".git" in dirpath:
                    continue
                for fn in filenames:
                    if any(fn.endswith(ext) for ext in TS_EXTENSIONS):
                        rel = os.path.relpath(os.path.join(dirpath, fn), repo)
                        all_files.append(rel)
                        if test_regex.search(rel):
                            test_files.append(rel)

        # 2. Build direct import graph for all files
        direct_graph = {}
        for f in all_files:
            direct_graph[f] = extract_imports(f, repo, tsconfig_paths, pkg_map)

        # 3. Compute transitive closure for each test file
        test_closures = {}
        for tf in test_files:
            visited = set()
            stack = list(direct_graph.get(tf, ()))
            while stack:
                curr = stack.pop()
                if curr in visited:
                    continue
                visited.add(curr)
                stack.extend(direct_graph.get(curr, ()))
            test_closures[tf] = sorted(visited)

        return test_closures

    def classify_change(self, repo, path, old_source, new_source):
        # Default: module_level unless proven body-only
        return "module_level"

    def collect_map(self, artifacts_dir):
        """Parse Vitest V8 JSON coverage (coverage-final.json)."""
        cov_file = os.path.join(artifacts_dir, "coverage-final.json")
        if not os.path.exists(cov_file):
            return {}
        try:
            with open(cov_file, "r") as f:
                data = json.load(f)
            # data maps absolute file path -> coverage details
            return {k: [line for line, count in v.get("s", {}).items() if count > 0] for k, v in data.items()}
        except Exception:
            return {}

    def test_seconds(self, artifacts_dir):
        timing_file = os.path.join(artifacts_dir, "test_seconds.json")
        if os.path.exists(timing_file):
            with open(timing_file) as f:
                return json.load(f)
        return {}
