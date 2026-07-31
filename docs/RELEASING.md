# Releasing Satay

Satay publishes to PyPI from GitHub Actions using **OIDC trusted publishing** (ADR-0019).
There is no API token in the repository, in a secret, or on a developer's machine. The
release workflow is `.github/workflows/release.yml`.

The rule the pipeline enforces: **never publish a commit that CI has not already proven
green.** A release job resolves the tagged SHA, waits for the `ci.yml` run for *that exact
SHA*, and refuses to build unless it succeeded. Publishing additionally requires the SHA
to be an ancestor of `main` and the tag to match the version in `pyproject.toml`.

---

## 1. One-time setup — Jian must do this by hand

**The workflow cannot publish anything until these steps exist.** OIDC has no fallback:
without a registered trusted publisher, PyPI rejects the exchange and the job fails.

### 1a. Register the trusted publisher on PyPI — **done, do not redo**

This started as a **pending publisher** (the flow that creates the project on first
upload). Publishing `0.1.0a1` converted it into an ordinary trusted publisher on the
`satay` project, so it no longer appears under *Add a new pending publisher* — it lives at
<https://pypi.org/manage/project/satay/settings/publishing/>.

Nothing to do here but confirm it is still listed, with exactly these values:

| Field                | Value            |
| -------------------- | ---------------- |
| PyPI Project Name    | `satay`          |
| Owner                | `leejianrong`    |
| Repository name      | `satay-runtime`  |
| Workflow name        | `release.yml`    |
| Environment name     | `pypi`           |

The values are matched literally — `release.yml` is the **filename**, not the workflow's
display name (`Release`), and the environment name must match the `environment: name:` on
the `publish-pypi` job. If any of them ever drifts, PyPI rejects the OIDC exchange and the
publish job fails with a permission error rather than anything mentioning the mismatch.

### 1b. Register the trusted publisher on TestPyPI

Same form at <https://test.pypi.org/manage/account/publishing/>, identical values except:

| Field            | Value      |
| ---------------- | ---------- |
| Environment name | `testpypi` |

This one only gates the rehearsal path. Skip it and `target=testpypi` will fail while real
releases still work — but then you cannot rehearse the upload, which is the point.

### 1c. Create the two GitHub environments — **done, do not redo**

Both environments now exist in **Settings → Environments** on `leejianrong/satay-runtime`,
configured as follows:

- **`pypi`** — required reviewer set, so every real upload pauses for a human click.
  **Deployment branches and tags** restricted to the tag pattern `v*`.
- **`testpypi`** — **no protection rules, deliberately.** Its publish job runs from branch
  `main` via `workflow_dispatch`, so a tag-only deployment policy would block every
  rehearsal. Leave it open.

The environment names match §1a/§1b exactly. A job referencing a nonexistent environment
fails to start, which is why this was the blocking step.

### 1d. Confirm you still own the name — **done, do not redo**

<https://pypi.org/project/satay/> has been ours since `0.1.0a1` went up on 2026-07-31, and
a PyPI project name cannot be transferred away from under you. A glance at that page before
a release is enough to confirm it, and it doubles as a check that the last release is where
you think it is.

`satay-runtime` — the repository name — is *not* reserved on PyPI and we do not publish
under it. Worth claiming eventually so nobody squats a lookalike; nothing about a release
depends on it.

---

## 2. Rehearse before you release

**Both rehearsal paths have been run green, and `satay` is published on PyPI** — `v0.1.0a1`
was the first release. The pipeline is proven end to end; this section is the procedure for
whatever version you are about to ship, not outstanding work.

Two rehearsal paths, both via **Actions → Release → Run workflow**. Neither needs a tag,
so you can point them at a branch.

| `target`   | What happens                                                              |
| ---------- | ------------------------------------------------------------------------- |
| `dry-run`  | Gate + build + verify the artifacts. Uploads nothing. The default.         |
| `testpypi` | The same, then uploads to TestPyPI through the identical OIDC publish step. |

A dry run proves everything except the upload: the CI-green gate, the Studio bundle build,
`uv build`, `twine check --strict`, the wheel/sdist content assertions, and clean-venv
installs of both `satay` and `satay[studio]`. The wheel listing is pasted into the run
summary.

After a `testpypi` run, install from TestPyPI to confirm the artifact is real. Pin the
version you just rehearsed — `0.1.0a2` here:

```bash
uv venv /tmp/satay-testpypi
uv pip install --python /tmp/satay-testpypi/bin/python \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  'satay==0.1.0a2'
/tmp/satay-testpypi/bin/satay --help
```

The extra index is needed because Satay's `studio` dependencies (FastAPI, uvicorn, …) live
on real PyPI, not TestPyPI.

---

## 3. Release

The version lives in `pyproject.toml` and the tag must match it — the workflow fails
otherwise. `pyproject.toml` currently says `0.1.0a2`, so the tag is `v0.1.0a2`.

```bash
# From a clean checkout of merged main, with the release commit already green in CI.
git switch main
git pull --ff-only
git log --oneline -1          # confirm this is the SHA you mean to ship

git tag -a v0.1.0a2 -m "satay 0.1.0a2"
git push origin v0.1.0a2
```

Pushing the tag starts **Release**. It will:

1. Gate — tag/version match, CI green for the SHA, SHA on `main`.
2. Build — pnpm build of the Studio SPA (pinned Node from `studio/.nvmrc`), `uv build`,
   `twine check --strict`, assert the bundle is in both artifacts, clean-venv install
   smokes.
3. Pause on the `pypi` environment for your approval.
4. Publish to PyPI over OIDC.

To release the *next* version, bump `version` in `pyproject.toml` (run `uv lock` so
`uv.lock` follows — CI installs with `--frozen`), merge that through a PR, then tag. Both
halves matter: an unbumped `pyproject.toml` fails the `guard` job on the tag/version match,
and a stale `uv.lock` fails earlier and less legibly, at `uv sync --frozen`, complaining
that the lockfile is out of date without mentioning the version at all.

### If it goes wrong

A PyPI version number is permanent. It cannot be deleted and cannot be reused — a broken
upload means yanking it (`satay` project page → Manage → Yank) and shipping a new version.
That is why the `pypi` environment has a required reviewer and why you rehearse on
TestPyPI first.

---

## 4. Verify after a release

Two checks, both from a clean virtualenv against the real index.

**The core install runs the quickstart.** This is the crash-recovery headline from V1, run
against a lean install with no dev dependencies:

```bash
uv venv /tmp/satay-verify
uv pip install --python /tmp/satay-verify/bin/python 'satay==0.1.0a2'
/tmp/satay-verify/bin/satay --help

# The version the package reports must equal the tag you just shipped (KAN-447).
/tmp/satay-verify/bin/python -c "
import importlib.metadata as m, satay
assert satay.__version__ == m.version('satay') == '0.1.0a2', satay.__version__
print('version ok:', satay.__version__)"

curl -fsSL \
  https://raw.githubusercontent.com/leejianrong/satay-runtime/v0.1.0a2/examples/crash_recovery_demo.py \
  -o /tmp/crash_recovery_demo.py
/tmp/satay-verify/bin/python /tmp/crash_recovery_demo.py
```

Expect `version ok: 0.1.0a2`, then `phase 2: final result = 4 (expected 4)`, `step_one
executions = 1 (REUSED, still 1)`, and a timeline ending in `WorkflowCompleted` with a `⚡`
on `WorkflowResumed`. Also confirm the install stayed lean — `pip list` in that venv should
show `satay` and nothing else of substance (ADR-0013/0016).

**`pip install 'satay[studio]'` actually serves Studio.** The extra is worthless if the
prebuilt SPA did not ride along in the wheel:

```bash
uv venv /tmp/satay-studio
uv pip install --python /tmp/satay-studio/bin/python 'satay[studio]==0.1.0a2'
/tmp/satay-studio/bin/satay dev
```

`satay dev` binds `127.0.0.1:8787` and prints a tokenized Studio URL (ADR-0014). Open it —
you should get the Satay Studio SPA, not a JSON 404. The release job asserts the bundle is
present in the wheel and loadable from an installed package, but only this step proves the
served page renders.

---

## 5. How the pipeline is put together

`.github/workflows/release.yml`, five jobs:

| Job                | Role                                                                     |
| ------------------ | ------------------------------------------------------------------------ |
| `guard`            | Resolves the target; enforces tag/version match, CI-green-for-SHA, on-main. |
| `build`            | Builds the Studio bundle, packages, verifies, uploads the `dist` artifact. |
| `dry-run`          | Reports a successful rehearsal. Publishes nothing.                        |
| `publish-testpypi` | OIDC upload to TestPyPI, `skip-existing: true` so rehearsals can repeat.  |
| `publish-pypi`     | OIDC upload to PyPI. Gated by the `pypi` environment.                     |

Notes worth knowing:

- Only the two publish jobs hold `id-token: write`; everything else runs `contents: read`.
- The Studio SPA is rebuilt in the release job rather than trusted from `src/`, so the
  shipped bundle is provably built from the tagged `studio/` source (ADR-0013: prebuilt in
  CI, never at `pip install`). Vite's output is reproducible, so this is normally a no-op;
  the job warns if the committed bundle turns out to be stale.
- The wheel and sdist content assertions exist because the e2e Studio-serving tests
  *skip* when the bundle is missing rather than fail. Without these assertions a packaging
  regression would ship a `satay[studio]` that serves nothing, and no test would notice.
- `pypa/gh-action-pypi-publish` is referenced as `@release/v1`, the ref upstream maintains
  for this purpose. Pinning it to a commit SHA is a reasonable hardening follow-up.
