"""Language adapters: coverage collection, test timing, static import graphs, and change classification.

Every language adapter implements the LanguageAdapter protocol:
  collect_map(artifacts_dir) -> {test_file: [source_files]}
  test_seconds(artifacts_dir) -> {test_file: float}
  static_deps(repo_root, profile) -> {test_file: [source_files]}
  classify_change(diff_text) -> str ("body_only" | "module_level")

Engine code stays generic; all language-specific conventions live in their respective adapter.
"""
import importlib

_ADAPTERS = {
    "python": "engine.adapters.python",
    "typescript": "engine.adapters.typescript",
    "javascript": "engine.adapters.typescript",
}


def get_adapter(language):
    """Retrieve the LanguageAdapter for a given language declared in a repo profile."""
    lang = language.lower()
    if lang not in _ADAPTERS:
        raise NotImplementedError("No language adapter implemented for '%s'" % language)
    mod = importlib.import_module(_ADAPTERS[lang])
    return mod.Adapter()
