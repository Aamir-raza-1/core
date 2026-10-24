"""Compiled selection index (D9): the static import map + the runtime map in one compact file.

  python3 -m engine.cli index --profile P --static static.json --map map_testfiles.json.gz --out repo.idx

The static map is {test file: import closure} (hundreds of MB of JSON for a large repo); selection only
ever asks the reverse question (changed file -> test files), so the index stores the reverse adjacency:

  targets   interned test-file table: the static map's test files (sorted, ids 0..n_tests-1), then
            test files that only the runtime map knows
  importers changed file -> sorted test ids (static importers), CSR layout: keys + offsets into one blob
  runtime   same for the runtime map (None when no map was given)

File layout: MAGIC, 8-byte header length, marshal(header), raw blob of uint16/uint32 test ids.
The header is loaded eagerly (small); a key's ids are decoded from the blob only when asked (lazy), so a
query touches only the changed files. Stdlib only. `CompiledIndex` has the TestIndex interface used by
engine/select.py, so selection results are identical by construction (and checked by a golden contract).

The parsed profile is cached in the header with its sha256: a query whose profile bytes still match skips
the YAML parse (a subprocess when PyYAML is absent); any mismatch falls back to engine.profile.load.
"""
import array
import collections
import datetime
import hashlib
import json
import marshal
import os
import sys

from engine import __version__

MAGIC = b"CIDX1\n"
FORMAT = 1


def _sha256(path, chunk=1 << 22):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def _read_runtime(path):
    """{repo file: [test files]} from the analyzer's map (same reading as select.load_runtime_map)."""
    from engine.select import load_runtime_map  # noqa: PLC0415
    return load_runtime_map(path)


def _map_provenance(map_path, map_commit=None, map_date=None):
    """Where the runtime map came from: commit (given), run id (source_run_id.txt next to it), date."""
    if not map_path:
        return {"map_source": None, "map_commit": map_commit, "map_run_id": None, "map_date": map_date}
    run_id = None
    rid = os.path.join(os.path.dirname(os.path.abspath(map_path)), "source_run_id.txt")
    if os.path.exists(rid):
        with open(rid) as f:
            run_id = f.read().strip() or None
    if map_date is None:
        map_date = datetime.datetime.fromtimestamp(os.path.getmtime(map_path), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + " (file mtime)"
    return {"map_source": map_path, "map_commit": map_commit, "map_run_id": run_id, "map_date": map_date}


def compile_maps(static, runtime=None):
    """(static dict, runtime dict-of-iterables or None) -> (header fields, blob bytes)."""
    tests = sorted(static)
    extra = sorted({t for ts in (runtime or {}).values() for t in ts} - set(tests))
    targets = tests + extra
    tid = {t: i for i, t in enumerate(targets)}
    code = "H" if len(targets) < 1 << 16 else "I"
    rev = collections.defaultdict(list)
    for t in tests:  # tests ascending -> each list is already sorted
        i = tid[t]
        for d in static[t]:
            rev[d].append(i)
    blob = array.array(code)

    def csr(mapping, sort):
        keys = sorted(mapping)
        offs = array.array("I", [len(blob)])
        for k in keys:
            ids = mapping[k]
            blob.extend(sorted(set(ids)) if sort else ids)
            offs.append(len(blob))
        return keys, offs.tobytes()
    imp_keys, imp_offs = csr(rev, sort=False)
    rt = None
    if runtime is not None:
        rt = csr({k: [tid[t] for t in v] for k, v in runtime.items()}, sort=True)
    head = {"format": FORMAT, "code": code, "targets": targets, "n_tests": len(tests),
            "imp_keys": imp_keys, "imp_offs": imp_offs,
            "rt_keys": rt[0] if rt else None, "rt_offs": rt[1] if rt else None}
    return head, blob.tobytes()


def write(path, head, blob):
    hb = marshal.dumps(head)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(MAGIC)
        f.write(len(hb).to_bytes(8, "little"))
        f.write(hb)
        f.write(blob)
    os.replace(tmp, path)


def build(profile_path, static_path, map_path=None, out=None, map_commit=None, map_date=None):
    from engine import profile  # noqa: PLC0415
    with open(profile_path, "rb") as f:
        prof_bytes = f.read()
    prof = profile.load(profile_path)
    with open(static_path) as f:
        static = json.load(f)
    runtime = _read_runtime(map_path) if map_path else None
    head, blob = compile_maps(static, runtime)
    head["meta"] = dict(_map_provenance(map_path, map_commit, map_date), **{
        "engine_version": __version__, "repo": prof.get("repo"),
        "built_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "static_source": static_path, "static_sha256": _sha256(static_path), "static_bytes": os.path.getsize(static_path),
        "map_sha256": _sha256(map_path) if map_path else None,
        "profile_sha256": hashlib.sha256(prof_bytes).hexdigest(),
        "test_files": head["n_tests"], "runtime_only_test_files": len(head["targets"]) - head["n_tests"],
        "importer_keys": len(head["imp_keys"]), "runtime_keys": len(head["rt_keys"]) if head["rt_keys"] is not None else None,
    })
    head["profile"] = prof
    if out:
        write(out, head, blob)
    return head


class _Lazy:
    """Read-only {path: set(test files)} over one CSR section; decodes a key's ids on first access."""
    __slots__ = ("_pos", "_offs", "_blob", "_code", "_targets", "_cache")

    def __init__(self, keys, offs, blob, code, targets):
        self._pos = dict(zip(keys, range(len(keys))))
        self._offs = array.array("I")
        self._offs.frombytes(offs)
        self._blob, self._code, self._targets, self._cache = blob, code, targets, {}

    def __contains__(self, k):
        return k in self._pos

    def __len__(self):
        return len(self._pos)

    def __iter__(self):
        return iter(self._pos)

    def __getitem__(self, k):
        if k not in self._cache:
            i = self._pos[k]
            a = array.array(self._code)
            w = a.itemsize
            a.frombytes(self._blob[self._offs[i] * w:self._offs[i + 1] * w])
            t = self._targets
            self._cache[k] = {t[j] for j in a}
        return set(self._cache[k])  # a copy: callers may mutate (TestIndex hands out fresh sets too)

    def get(self, k, default=None):
        return self[k] if k in self._pos else default


class CompiledIndex:
    """Drop-in for engine.select.TestIndex, loaded from a compiled .idx file."""

    def __init__(self, path, seconds=None):
        with open(path, "rb") as f:
            if f.read(len(MAGIC)) != MAGIC:
                raise ValueError("%s: not an engine index (bad magic)" % path)
            n = int.from_bytes(f.read(8), "little")
            head = marshal.loads(f.read(n))
            blob = f.read()
        if head.get("format") != FORMAT:
            raise ValueError("%s: index format %r, engine reads %d (rebuild with `engine.cli index`)" % (path, head.get("format"), FORMAT))
        self.meta, self.profile = head["meta"], head.get("profile")
        targets = head["targets"]
        self.test_files = targets[:head["n_tests"]]
        self.test_set = set(self.test_files)
        self.importers = _Lazy(head["imp_keys"], head["imp_offs"], blob, head["code"], targets)
        self.runtime = None if head["rt_keys"] is None else _Lazy(head["rt_keys"], head["rt_offs"], blob, head["code"], targets)
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

    def cached_profile(self, profile_path):
        """The profile parsed at build time if the file's bytes are unchanged, else None."""
        if not self.profile:
            return None
        with open(profile_path, "rb") as f:
            same = hashlib.sha256(f.read()).hexdigest() == self.meta.get("profile_sha256")
        return self.profile if same else None


def main(argv=None):  # `python3 -m engine.index ...` == `engine.cli index ...`
    from engine import cli  # noqa: PLC0415
    return cli.main(["index"] + list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    sys.exit(main())
