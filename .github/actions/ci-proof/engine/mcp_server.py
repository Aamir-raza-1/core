"""Model Context Protocol (MCP) server for ci-proof engine.

Provides AI agent tools over stdio (JSON-RPC 2.0, MCP specification 2024-11-05):
- affected_tests: deterministic change-impact test selection via engine.select / engine.index
- audit_repo: CI waste & full-suite over-execution audit via engine.audit
- flaky_summary: flaky test episode & cost analysis via engine.flaky
"""
import datetime
import json
import os
import sys
import traceback

from engine import __version__, profile

SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26")

TOOLS = [
    {
        "name": "affected_tests",
        "description": (
            "Select the exact test files affected by changed files using the deterministic "
            "ci-proof engine (profile rules + compiled index or static/runtime dependency maps)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "changed_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of relative file paths modified in the workspace",
                },
                "body_only_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional subset of changed files whose edits are function-body only",
                },
                "profile": {
                    "type": "string",
                    "description": "Path to per-repo profile YAML/JSON (optional if index has a cached profile)",
                },
                "index": {
                    "type": "string",
                    "description": "Path to compiled .idx file from `ci-proof index` (replaces static/map)",
                },
                "static": {
                    "type": "string",
                    "description": "Path to static import map JSON {test_file: [repo_files]}",
                },
                "map": {
                    "type": "string",
                    "description": "Optional path to runtime trace map (map_testfiles.json.gz)",
                },
                "map_commit": {
                    "type": "string",
                    "description": "Optional commit SHA at which the runtime map was traced",
                },
                "seconds": {
                    "type": "string",
                    "description": "Optional path to per-test-file duration JSON",
                },
                "repo_root": {
                    "type": "string",
                    "description": "Optional path to repository checkout to build static import map on the fly when index/static are not pre-built",
                },
                "explain": {
                    "type": "boolean",
                    "description": "Include typed per-test decisions (MUST_RUN | SAFE_TO_SKIP | UNCERTAIN) and proof",
                    "default": False,
                },
                "out": {
                    "type": "string",
                    "description": "Optional path to write explain decisions JSON (when explain=true)",
                },
            },
            "required": ["changed_files"],
        },
    },
    {
        "name": "audit_repo",
        "description": "Run a CI history audit (runs, durations, failure/rerun rates, full-suite share) via engine.audit.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "enum": ["github", "gitlab", "prow", "buildkite", "azure", "jenkins"],
                    "description": "CI provider platform",
                    "default": "github",
                },
                "project": {
                    "type": "string",
                    "description": "Project identifier, e.g. python/mypy or vercel/next.js",
                },
                "days": {
                    "type": "integer",
                    "description": "Number of full days in audit window before end date",
                    "default": 7,
                },
                "end": {
                    "type": "string",
                    "description": "Exclusive end date YYYY-MM-DD (optional; defaults to current UTC date)",
                },
                "test_job_regex": {
                    "type": "string",
                    "description": "Regex matching test job names",
                },
                "full_run_rule": {
                    "type": "string",
                    "description": "Regex matching job names that indicate a full-suite run",
                },
                "workflows": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional workflow filenames to filter (GitHub)",
                },
                "job": {
                    "type": "string",
                    "description": "Optional Prow job name (when provider=prow)",
                },
                "out": {
                    "type": "string",
                    "description": "Optional output JSON path",
                },
            },
            "required": ["project"],
        },
    },
    {
        "name": "flaky_summary",
        "description": "Detect flaky CI runs (fail-then-pass on same workflow + commit), failing test IDs, categories, and compute cost via engine.flaky.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "enum": ["github"],
                    "description": "CI provider platform (github)",
                    "default": "github",
                },
                "project": {
                    "type": "string",
                    "description": "GitHub repository, e.g. python/mypy or vercel/next.js",
                },
                "days": {
                    "type": "integer",
                    "description": "Number of full days in audit window before end date",
                    "default": 7,
                },
                "end": {
                    "type": "string",
                    "description": "Exclusive end date YYYY-MM-DD (optional; defaults to current UTC date)",
                },
                "workflows": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional workflow filenames to filter",
                },
                "events": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional trigger events to filter",
                },
                "test_job_regex": {
                    "type": "string",
                    "description": "Regex for test jobs",
                    "default": "(?i)test",
                },
                "parsers": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["pytest", "vitest", "jest"]},
                    "description": "Log parsers to apply",
                },
                "out": {
                    "type": "string",
                    "description": "Optional output JSON path",
                },
            },
            "required": ["project"],
        },
    },
]


def _load_selection_engine(args):
    """Build (sel, rules, idx, prof) using the real engine (CompiledIndex, TestIndex, or live static_imports)."""
    from engine import select as sel  # noqa: PLC0415

    seconds = None
    if args.get("seconds"):
        with open(args["seconds"]) as f:
            seconds = json.load(f)

    prof_path = args.get("profile")
    index_path = args.get("index")
    static_path = args.get("static")
    map_path = args.get("map")
    repo_root = args.get("repo_root")

    if index_path:
        if static_path or map_path:
            raise ValueError("--index replaces --static/--map (the maps are compiled into it)")
        from engine.index import CompiledIndex  # noqa: PLC0415

        idx = CompiledIndex(index_path, seconds)
        if prof_path:
            prof = idx.cached_profile(prof_path) or profile.load(prof_path)
        elif idx.profile:
            prof = idx.profile
        else:
            raise ValueError("profile is required when index has no embedded profile")
        idx.map_source, idx.map_commit = idx.meta.get("map_source"), idx.meta.get("map_commit")
        return sel, sel.Rules(prof), idx, prof

    if static_path:
        if not prof_path:
            raise ValueError("profile is required when using static map")
        prof = profile.load(prof_path)
        rules = sel.Rules(prof)
        with open(static_path) as f:
            static = json.load(f)
        runtime = sel.load_runtime_map(map_path) if map_path else None
        idx = sel.TestIndex(static, runtime, seconds)
        idx.map_source, idx.map_commit = map_path, args.get("map_commit")
        return sel, rules, idx, prof

    if repo_root:
        if not prof_path:
            raise ValueError("profile is required when building static map from repo_root")
        prof = profile.load(prof_path)
        rules = sel.Rules(prof)
        lang = (prof.get("language") or "python").lower()
        if lang == "python":
            from engine import static_imports  # noqa: PLC0415

            static, _ = static_imports.build(
                repo_root,
                prof["source_roots"],
                prof["test_roots"],
                sel.RUNNERS[prof["test_runner"]]["test_file"],
                prof.get("import_roots"),
            )
        else:
            from engine.adapters import get_adapter  # noqa: PLC0415

            static = get_adapter(lang).static_deps(repo_root, prof)
        runtime = sel.load_runtime_map(map_path) if map_path else None
        idx = sel.TestIndex(static, runtime, seconds)
        idx.map_source, idx.map_commit = map_path, args.get("map_commit")
        return sel, rules, idx, prof

    raise ValueError("Provide 'index', 'static' (+ 'profile'), or 'repo_root' (+ 'profile')")


def handle_affected_tests(args):
    changed = list(args.get("changed_files") or [])
    body_only = list(args.get("body_only_files") or [])
    files = changed + [f for f in body_only if f not in changed]

    if not files:
        return {
            "status": "no_changes",
            "full": False,
            "selected_tests": [],
            "affected_test_targets": [],
            "selected_count": 0,
            "reasons": {},
            "changed_files_count": 0,
            "body_only_count": 0,
            "message": "No changed files provided.",
        }

    sel, rules, idx, prof = _load_selection_engine(args)
    kinds = {f: "body-only" for f in body_only}

    if args.get("explain"):
        exp = sel.explain(
            files,
            kinds,
            rules,
            idx,
            map_source=getattr(idx, "map_source", None),
            map_commit=getattr(idx, "map_commit", None),
        )
        if args.get("out"):
            os.makedirs(os.path.dirname(os.path.abspath(args["out"])), exist_ok=True)
            with open(args["out"], "w") as f:
                f.write(json.dumps(exp, separators=(",", ":")) + "\n")
        target_list = exp["selected"]
        full = exp["full"]
        reasons = exp["reasons"]
        share = 100.0 if full else idx.share(set(target_list))
        res = {
            "status": "success",
            "schema": exp["schema"],
            "full": bool(full),
            "selected_tests": target_list,
            "affected_test_targets": target_list,
            "selected_count": len(target_list),
            "total_test_files": len(idx.test_files),
            "share_pct": share,
            "reasons": reasons,
            "counts": exp["counts"],
            "changed_files_count": len(files),
            "body_only_count": len(body_only),
            "profile_used": prof.get("repo") or args.get("profile"),
            "decisions": exp["decisions"],
        }
        return res

    selected, full, reasons_counter = sel.select(files, kinds, rules, idx)
    target_list = list(idx.test_files) if full else sorted(selected)
    share = 100.0 if full else idx.share(selected)
    return {
        "status": "success",
        "full": bool(full),
        "selected_tests": target_list,
        "affected_test_targets": target_list,
        "selected_count": len(target_list),
        "total_test_files": len(idx.test_files),
        "share_pct": share,
        "reasons": dict(reasons_counter),
        "changed_files_count": len(files),
        "body_only_count": len(body_only),
        "profile_used": prof.get("repo") or args.get("profile"),
    }


def _ensure_writable_audit_dir(audit_mod):
    """Ensure engine.audit has a writable AUDITS directory when installed outside the repo checkout."""
    if os.path.isdir(audit_mod.AUDITS) and os.access(audit_mod.AUDITS, os.W_OK):
        return
    parent = os.path.dirname(audit_mod.AUDITS)
    if os.path.isdir(parent) and os.access(parent, os.W_OK):
        return
    fallback = os.environ.get("CI_PROOF_CACHE_DIR") or os.path.join(
        os.path.expanduser("~"), ".cache", "ci-proof", "audits"
    )
    os.makedirs(fallback, exist_ok=True)
    audit_mod.AUDITS = fallback


def handle_audit_repo(args):
    from engine import audit  # noqa: PLC0415
    from engine.audit.common import Window  # noqa: PLC0415

    provider = args.get("provider", "github")
    project = args.get("project", "")
    if not project:
        raise ValueError("project is required")
    days = int(args.get("days", 7))
    end = args.get("end")

    # If no explicit end date is passed and a pre-computed audit summary exists locally while
    # raw API cache is absent (e.g. git-tracked summary in repo without gitignored cache),
    # use the cached window if raw cache exists, else return the verified audit summary.
    cached_path = audit.out_path(provider, project)
    raw_cache = audit.cache_dir(provider, project)
    if not end and os.path.isfile(cached_path) and not os.path.isdir(raw_cache):
        with open(cached_path) as f:
            return json.load(f)

    if not end and os.path.isfile(cached_path) and os.path.isdir(raw_cache):
        with open(cached_path) as f:
            cached_data = json.load(f)
        win_info = cached_data.get("window") or {}
        end = win_info.get("end_exclusive")
        if "days" not in args and win_info.get("days"):
            days = int(win_info["days"])
        params = (cached_data.get("extra") or {}).get("params") or {}
        if not args.get("workflows") and params.get("workflows"):
            args = dict(args, workflows=params["workflows"])
        if not args.get("test_job_regex") and params.get("test_job_regex"):
            args = dict(args, test_job_regex=params["test_job_regex"])
        if not args.get("full_run_rule") and params.get("full_run_rule"):
            args = dict(args, full_run_rule=params["full_run_rule"])

    if not end:
        end = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    _ensure_writable_audit_dir(audit)
    opts = {}
    if args.get("workflows"):
        opts["workflows"] = list(args["workflows"])
    if args.get("job"):
        opts["job"] = args["job"]

    summary = audit.run(
        provider,
        project,
        Window(days, end),
        test_job_regex=args.get("test_job_regex"),
        full_run_rule=args.get("full_run_rule"),
        **opts,
    )
    if args.get("out"):
        os.makedirs(os.path.dirname(os.path.abspath(args["out"])), exist_ok=True)
        with open(args["out"], "w") as f:
            json.dump(summary, f, indent=1)
            f.write("\n")
    return summary


def handle_flaky_summary(args):
    from engine import audit, flaky  # noqa: PLC0415
    from engine.audit.common import Window, slug  # noqa: PLC0415

    provider = args.get("provider", "github")
    if provider != "github":
        raise NotImplementedError("flaky: provider %r not implemented yet (github only)" % provider)
    project = args.get("project", "")
    if not project:
        raise ValueError("project is required")
    days = int(args.get("days", 7))
    end = args.get("end")

    cached_path = os.path.join(audit.AUDITS, "flaky__%s__%s.json" % (provider, slug(project)))
    raw_cache = audit.cache_dir(provider, project)
    if not end and os.path.isfile(cached_path) and not os.path.isdir(raw_cache):
        with open(cached_path) as f:
            return json.load(f)

    if not end and os.path.isfile(cached_path) and os.path.isdir(raw_cache):
        with open(cached_path) as f:
            cached_data = json.load(f)
        win_info = cached_data.get("window") or {}
        end = win_info.get("end_exclusive")
        if "days" not in args and win_info.get("days"):
            days = int(win_info["days"])
        if not args.get("workflows") and isinstance(cached_data.get("workflows"), list):
            args = dict(args, workflows=cached_data["workflows"])
        if not args.get("parsers") and cached_data.get("parsers"):
            args = dict(args, parsers=cached_data["parsers"])

    if not end:
        end = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    _ensure_writable_audit_dir(audit)
    parsers = tuple(args.get("parsers") or ("pytest", "vitest"))
    report = flaky.run_github(
        project,
        Window(days, end),
        audit.cache_dir(provider, project),
        workflows=args.get("workflows"),
        test_job_regex=args.get("test_job_regex", flaky.DEFAULT_TEST_JOB_REGEX),
        parsers=parsers,
        events=args.get("events"),
    )
    if args.get("out"):
        os.makedirs(os.path.dirname(os.path.abspath(args["out"])), exist_ok=True)
        with open(args["out"], "w") as f:
            json.dump(report, f, indent=1)
            f.write("\n")
    return report


DISPATCH = {
    "affected_tests": handle_affected_tests,
    "audit_repo": handle_audit_repo,
    "flaky_summary": handle_flaky_summary,
}


def handle_request(req):
    """Process one JSON-RPC 2.0 request dict and return response dict (or None for notifications)."""
    if not isinstance(req, dict):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }

    method = req.get("method")
    # Notifications have no "id" member and must not receive a response.
    if "id" not in req or method in ("notifications/initialized", "notifications/cancelled") or (
        isinstance(method, str) and method.startswith("notifications/")
    ):
        return None

    req_id = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        client_version = params.get("protocolVersion")
        proto = client_version if client_version in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": proto,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ci-proof", "version": __version__},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": []}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": []}}
    if method == "tools/call":
        tname = params.get("name")
        targs = params.get("arguments") or {}
        fn = DISPATCH.get(tname)
        if not fn:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": "Unknown tool: %s" % tname},
            }
        try:
            res_val = fn(targs)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(res_val, indent=2)}],
                    "isError": False,
                },
            }
        except Exception as e:  # noqa: BLE001
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32000,
                    "message": str(e),
                    "data": traceback.format_exc(),
                },
            }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": "Method not found: %s" % method},
    }


def run_stdio_server(instream=None, outstream=None):
    """Run MCP JSON-RPC 2.0 protocol loop on stdin / stdout."""
    inp = instream or sys.stdin
    out = outstream or sys.stdout
    while True:
        line = inp.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        resp = handle_request(req)
        if resp is not None:
            out.write(json.dumps(resp) + "\n")
            out.flush()


if __name__ == "__main__":
    run_stdio_server()
