# CI Proof (working name): run the tests a change can break, and prove nothing was missed

## The problem
Most CI systems run every test on every pull request. Teams pay for it twice: in compute, and in developers waiting for a green check. AI coding tools are now multiplying the number of pull requests, so this cost is growing. On top of that, flaky tests (tests that fail, then pass on a re-run with no code change) turn red builds into noise.

## What it does
1. **Free CI audit (read-only).** It reads a project's CI history through the provider API and reports:
   - how often the full test suite runs;
   - which CI rules waste compute;
   - which tests are flaky and what they cost;
   - free configuration fixes;

   all compared side by side: today vs. with config fixes vs. with selection. No access to the source code is needed. Providers: GitHub Actions, GitLab CI and Prow.
2. **Test selection.** It builds a map of which test touches which file. Python uses per-test coverage plus a file-open trace; Go uses its package import graph. For each change, it picks only the tests that can be affected. Each test gets a typed decision (MUST_RUN, SAFE_TO_SKIP or UNCERTAIN), with the evidence behind it. A decision takes about 40 ms. The engine is deterministic: no AI model decides what to skip.
3. **Proof of safety.**
   - **Planted bugs:** we inject small bugs and check that the selected tests still catch them.
   - **Passive shadow:** our selection is replayed against the project's *real* CI failures from public logs. A real failure outside our selection counts as a miss, and every miss is diagnosed.

## What it is not (today)
- Not a hosted product: there is no GitHub App, dashboard or billing yet. It is a command-line engine plus reports.
- Selection works for Python, Go, and TypeScript (Vitest/Jest static + coverage maps).
- It does not fix flaky tests. It finds them, names them and prices them.
- It does not change anyone's CI. Every public result is read-only. Forks run on the owner's personal account, and nothing is posted to any project.
- Savings are **replay estimates**, not live measurements. Live shadow mode inside a customer's CI is the next step.

## Feature & Packaging Status
Every capability and packaging method is listed below with its real verification status (unimplemented items are marked **Planned** and never presented as live commands):

| Feature / Integration | Status | Verification / Notes |
|---|---|---|
| CI History Audit (`ci-proof audit`: GitHub, GitLab, Prow) | **Implemented** | Verified by contracts V14, V33, V38, V41 |
| Audit adapters: Buildkite, Azure DevOps, Jenkins | **Planned** | Deferred to next factory version (RQ25) |
| Flaky Test Detector (`ci-proof flaky`: GitHub) | **Implemented** | Verified by contract V25 (pytest, vitest, jest, junit, gojunit parsers) |
| Comparative Audit Report (`ci-proof report`: Markdown + HTML) | **Implemented** | Verified by contract V26 (labelled footnotes, self-contained HTML) |
| Change-Aware Selection (`ci-proof select`, `affected-tests`, `index`) | **Implemented** | Python, Go, TypeScript/Vitest; verified by contracts V4, V6, V23, V34, V39, V42 |
| Safety Verification (`ci-proof verify`) & Passive Shadow (`ci-proof shadow`) | **Implemented** | Verified by contracts V24, V32, V35, V40 |
| Tracer CLI (`ci-proof trace`) | **Planned (R1)** | Exits with code 2; tracer scripts currently live in the fork's `script/trace/` |
| Local Installer & Uninstaller (`scripts/install.sh`, `scripts/uninstall.sh`) | **Implemented** | Local checkout install + zero-residue uninstall (contract V44); remote `curl` installer **Planned (D1)** |
| Python Package (`pip install .` / `pipx install .`) | **Implemented** | Python ≥ 3.9, stdlib-only core (`pip install ".[yaml]"` for PyYAML) |
| Docker Runner (`Dockerfile`, local build) | **Implemented (local build)** | `docker build -t ci-proof:local .`; published GHCR image **Planned (D1)** |
| MCP Server (`ci-proof mcp`) | **Implemented** | JSON-RPC 2.0 over stdio |
| GitHub Action (`action.yml`) | **Local / vendored** | Repo is currently private; external repos must vendor `.github/actions/ci-proof/` until public release **D1** |
| Hosted SaaS (GitHub App, dashboard, Stripe billing) | **Planned (D2–D5)** | Not yet built |

## Results so far (every number is labelled and can be re-checked: see VERIFY.md)
| Repo | Language | Saving (estimate) | Compared with | Safety evidence |
|---|---|---|---|---|
| home-assistant/core | Python | 26% of full-run test time | its own rules | 21 planted bugs: 16 caught by the full suite, 0 missed by selection |
| pydantic/pydantic | Python | 25–32% of pytest time (≈17% of all test-job time) | runs everything | 138 planted bugs, 2 samples: 0 missed |
| langchain-ai/langchain | Python monorepo | 45–53% | its own package filter | 94 planted bugs: 1 reported miss (order-dependent) |
| openedx/openedx-platform | Python / Django | 29–35% | runs everything | **90 days of real CI failures: 2,782 of 2,785 caught** (in-sample) |
| kubernetes/kubernetes | Go | ≈56% (untuned window) | runs everything | **89 days of real CI failures: 520 caught, 16 misses, each diagnosed** (in-sample) |
| supabase/supabase | TypeScript monorepo | ≈88% of Studio test time (estimate) | paths-filter (all 769 files on Studio touch) | 7-day replay (619 runs); safety unverified (planted bugs pending) |
| python/mypy | Python | audit only | — | — |
| A private enterprise TS monorepo | TypeScript | 42% (vs 38% from its build tool's own option) | its build tool | 20 planted bugs: 0 missed |

**Honest limits:**
- The shadow results were tuned on the same window they were measured on. Out-of-sample re-runs are due after 2026-10-18.
- The planted-bug samples are small.
- No public project pays for CI minutes, so their savings are developer waiting time, not dollars.

## Quickstart & Installation

Zero code modification and zero SDK imports in your repository. Test selection (`ci-proof index`, `affected-tests`, `select`, `verify`) runs locally or in CI with zero network egress; read-only CI history commands (`ci-proof audit`, `flaky`, `shadow`) query the configured CI provider API.

### 1. Local Installation (`scripts/install.sh` or `pip` / `pipx`)
From a local checkout of this repository:
```bash
# Option A: Standalone wrapper installed to ~/.ci-proof and symlinked at ~/.local/bin/ci-proof
bash scripts/install.sh

# Option B: Editable or standard pip / pipx install (add [yaml] on Linux if Ruby is not installed)
pip install ".[yaml]"
```
Verify the installation and version:
```bash
ci-proof --version   # prints: ci-proof 0.1.0
ci-proof --help
```
To cleanly remove everything installed by `scripts/install.sh`:
```bash
bash scripts/uninstall.sh
```

### 2. Live Test Selection Example

**A. Self-contained demo (works on any fresh clone with zero external files):**
```bash
TMP_DEMO="$(mktemp -d)"
printf '{"tests/components/hue/test_light.py":["homeassistant/components/hue/light.py"],"tests/components/zwave/test_init.py":["homeassistant/components/zwave/init.py"]}\n' > "$TMP_DEMO/static.json"
ci-proof index --profile profiles/home-assistant__core.yaml --static "$TMP_DEMO/static.json" --out "$TMP_DEMO/demo.idx"
ci-proof affected-tests --profile profiles/home-assistant__core.yaml --index "$TMP_DEMO/demo.idx" --changed homeassistant/components/hue/light.py
rm -rf "$TMP_DEMO"
```
*Output:*
```text
tests/components/hue/test_light.py
```

**B. Full Home Assistant Phase 1 index (when `experiments/home-assistant/phase1/` maps are present locally):**
```bash
# Compile static + runtime maps into a fast binary index (.idx is git-ignored)
ci-proof index \
  --profile profiles/home-assistant__core.yaml \
  --static experiments/home-assistant/phase1/static.json \
  --map experiments/home-assistant/phase1/full/map_testfiles.json.gz \
  --out experiments/home-assistant/phase1/ha.idx

# Query affected tests for a modified leaf component file (~50 ms)
ci-proof affected-tests \
  --profile profiles/home-assistant__core.yaml \
  --index experiments/home-assistant/phase1/ha.idx \
  --changed homeassistant/components/hue/light.py
```
*Output:*
```text
tests/components/hue/test_bridge.py
tests/components/hue/test_init.py
tests/components/hue/test_light_v1.py
tests/components/hue/test_light_v2.py
tests/test_loader.py
```
*(5 of 2,756 test files selected for this single-file change; across the 40-PR Home Assistant replay window in `VERIFY.md`, selection saves **26.3%** of full-run test time, measured via contract V6.)*

### 3. AI Agent Integration: Claude Code & Cursor (MCP Server)
After installing `ci-proof` onto your `PATH` (via `scripts/install.sh`, `pip`, or `pipx`), register the stdio MCP server in your client configuration:
```json
{
  "mcpServers": {
    "ci-proof": {
      "command": "ci-proof",
      "args": ["mcp"]
    }
  }
}
```

### 4. Docker Container (Local Build)
No pre-built container image is published to a public registry yet (planned for public release D1). Build and run the container locally from your checkout:
```bash
docker build -t ci-proof:local .
docker run --rm -v "$(pwd):/workspace" ci-proof:local --version
docker run --rm -v "$(pwd):/workspace" ci-proof:local audit \
  --provider github --project python/mypy --days 7 --end 2026-10-10
```

### 5. GitHub Actions Integration
Because this repository is currently **private** (pre-D1), external repositories cannot reference `uses: Aamir-raza-1/ci-casestudies@main` directly.
- **Inside this repository:** use `uses: ./`
- **In an external repository or fork:** vendor `action.yml`, `pyproject.toml`, `setup.py`, `engine/`, and `profiles/` into `.github/actions/ci-proof/` and reference `uses: ./.github/actions/ci-proof`:
```yaml
- name: Change-Aware Test Selection
  uses: ./.github/actions/ci-proof
  with:
    profile: profiles/home-assistant__core.yaml
    index: ha.idx
```

## How to check anything
- `make check`: runs structural and syntax checks.
- `make verify`: re-runs every automated behavioral proof.
- `ci-proof report`: generates an executive waste report (Markdown + HTML).
- [INSTALL.md](INSTALL.md): full customer installation guide, CLI reference, and cross-OS verification matrix.
- [VERIFY.md](VERIFY.md): repo by repo, what each number means, how it was computed, and how to check it by hand.
- [REVIEW.md](REVIEW.md): every decision made, with how to undo it.
- [OVERVIEW.md](OVERVIEW.md): the whole system end to end (layers, step-by-step walk-through, diagram, reading order).
- [SYSTEM.md](SYSTEM.md): how the self-running factory works (commands, checkpoints, prompts).
- [knowledge/business-case.md](knowledge/business-case.md): who would pay, and why.
