"""Python language adapter."""
import json
import os
from engine import static_imports
from engine.select import RUNNERS


class Adapter:
    def static_deps(self, repo, profile):
        runner_info = RUNNERS[profile["test_runner"]]
        graph, _ = static_imports.build(
            repo,
            profile["source_roots"],
            profile["test_roots"],
            runner_info["test_file"],
            profile.get("import_roots"),
        )
        return graph

    def classify_change(self, repo, path, old_source, new_source):
        return static_imports.classify(old_source, new_source, path)

    def collect_map(self, artifacts_dir):
        # Reads coverage json or coverage sqlite from artifacts
        cov_file = os.path.join(artifacts_dir, "coverage.json")
        if os.path.exists(cov_file):
            with open(cov_file) as f:
                return json.load(f)
        return {}

    def test_seconds(self, artifacts_dir):
        timing_file = os.path.join(artifacts_dir, "test_seconds.json")
        if os.path.exists(timing_file):
            with open(timing_file) as f:
                return json.load(f)
        return {}
