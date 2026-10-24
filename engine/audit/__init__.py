"""Free CI audit: one adapter per CI provider, one common summary (engine/audit/common.py).

Each provider module engine/audit/<name>.py defines
    fetch_runs(project, window, cache_dir, **opts) -> [normalized run]
Adapters are added in Stage A: gitlab (A3), buildkite + azure (A4), jenkins + github (A5).
"""
import importlib
import os

from . import common

PROVIDERS = ["github", "gitlab", "buildkite", "azure", "jenkins", "prow"]
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AUDITS = os.path.join(ROOT, "experiments", "_audits")


def adapter(provider):
    if provider not in PROVIDERS:
        raise ValueError("unknown provider %r (known: %s)" % (provider, ", ".join(PROVIDERS)))
    try:
        return importlib.import_module("engine.audit." + provider)
    except ModuleNotFoundError as e:
        if e.name == "engine.audit." + provider:
            raise NotImplementedError("audit adapter %r not implemented yet (Stage A, A3-A5)" % provider) from e
        raise


def cache_dir(provider, project):
    return os.path.join(AUDITS, "cache", provider, common.slug(project))


def out_path(provider, project):
    return os.path.join(AUDITS, "%s__%s.json" % (provider, common.slug(project)))


def run(provider, project, window, test_job_regex=None, full_run_rule=None, **opts):
    mod = adapter(provider)
    runs = mod.fetch_runs(project, window, cache_dir(provider, project), **opts)
    extra = common.breakdown(runs, test_job_regex=test_job_regex, full_run_rule=full_run_rule)
    extra["test_execution"] = common.test_execution(runs, test_job_regex)
    extra["params"] = {"workflows": opts.get("workflows"), "test_job_regex": test_job_regex, "full_run_rule": full_run_rule}
    return common.summarize(runs, window, provider, project, test_job_regex=test_job_regex,
                            full_run_rule=full_run_rule, sources=getattr(mod, "SOURCES", []),
                            method=getattr(mod, "METHOD", ""),
                            extra=extra)
