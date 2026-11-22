# doka-labs/workflows

Reusable GitHub Actions workflows shared by the doka-labs repositories. Each
workflow is maintained once here; repositories call it by commit SHA and keep
their own inputs.

## Calling a workflow

Pin the call to a full commit SHA and keep the version as a comment so
Dependabot can update it. Dependabot updates both the SHA and the comment of a
pinned call (for example `upload-sarif@9b325408... # v4.39.1` to
`@33d7b85d... # v4.39.3` in NestedSet pull request #34); it proposes versions
from this repository's tags, listed in `CHANGELOG.md`:

```yaml
jobs:
  dependency-review:
    uses: doka-labs/workflows/.github/workflows/dependency-review.yml@<commit-sha> # v0.1.0
```

## Tools

Workflows that need more than a few lines of logic check out `tools/` from the
exact commit of the workflow itself (`job.workflow_repository` at
`job.workflow_sha`), so a caller's pin covers the code that runs. The tools use
the Python standard library only. Their tests run with:

```sh
python3 -m unittest discover -s tools -p 'test_*.py' -t .
```

## Self check

`self-check.yml` runs on every pull request and push to `main`: actionlint
1.7.12 (downloaded from its release and checked against the published
checksum) and the tool tests on `ubuntu-26.04`. The tests include
`tools/repository_checks/`: every `actions/upload-artifact` step in this
repository must name individual text evidence files (`.json`, `.log`, `.trx`,
`.txt`, `.sql`, `.md`, `.xml`, `.csv`, `.sarif`), with no directories,
patterns or exclusions, and must not disable compression. Uploading whole tool
output directories carried the NuGet HTTP cache into audit evidence and, in
one repository, about 53 GB of uncompressed EF migration bundles into CI
evidence within 14 days.

## Workflows

### `dependency-audit.yml`

Audits every .NET project file of the calling repository (all `*.csproj`
outside `bin`, `obj`, `artifacts` and hidden directories, including projects
that are not part of a solution) for known direct and transitive
vulnerabilities. It restores with NuGet audit warnings as errors, lists
vulnerable packages as JSON and validates the listing before it reports a clean
result. The NuGet HTTP cache stays outside the uploaded evidence.

| Input | Default | Purpose |
| --- | --- | --- |
| `runs-on` | `ubuntu-26.04` | Runner label |
| `locked-mode` | `true` | Restore with `--locked-mode` |
| `retention-days` | `0` | Retention of the evidence artifact; `0` uses the repository or organization setting |

Caller:

```yaml
name: Dependency Audit

on:
  schedule:
    - cron: "35 4 * * *"
  workflow_dispatch:

permissions:
  contents: read

jobs:
  dependency-audit:
    uses: doka-labs/workflows/.github/workflows/dependency-audit.yml@<commit-sha> # v0.1.0
```

The tool lives in `tools/dependency_audit/`.

### `workflow-lint.yml`

Lints the calling repository's workflows and shell scripts: actionlint 1.7.12
(checksum-verified), zizmor 1.32.0 (installed with `pip --require-hashes` from
`tools/workflow_lint/zizmor-requirements.txt`, run `--offline`), and shellcheck
from the runner image for every tracked `*.sh`. actionlint reads the caller's
own `.github/actionlint.yaml`; a caller that uses `ubuntu-26.04`, the
`job.workflow_*` context, `concurrency.queue` or `uses: $/...` needs the same
entries as this repository's `.github/actionlint.yaml`.

```yaml
jobs:
  workflow-lint:
    uses: doka-labs/workflows/.github/workflows/workflow-lint.yml@<commit-sha> # v0.4.0
```

### `dependency-review.yml`

Reviews the dependency delta of a pull request with
`actions/dependency-review-action`. Fails on advisories at or above
`fail-on-severity` and on licenses outside `allow-licenses`. Packages listed in
`license-exceptions` are exempt from the license check only at their exact
reviewed versions; that check lives in `tools/dependency_review/`.

| Input | Default | Purpose |
| --- | --- | --- |
| `runs-on` | `ubuntu-26.04` | Runner label |
| `fail-on-severity` | `high` | Lowest failing advisory severity |
| `allow-licenses` | Apache-2.0, BSD-2-Clause, BSD-3-Clause, CC0-1.0, ISC, MIT, MIT-0, PostgreSQL, Unlicense | Allowed SPDX identifiers |
| `license-exceptions` | `pkg:nuget/Microsoft.Data.SqlClient.SNI.runtime@6.0.3`, `pkg:nuget/Microsoft.Identity.Client.NativeInterop@0.20.6`, `pkg:nuget/SQLite@3.53.4` | `pkg:nuget/Name@version` entries, comma-separated |

Caller with the defaults:

```yaml
name: Dependency Review

on:
  pull_request:

permissions:
  contents: read

concurrency:
  group: ${{ github.workflow }}-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  dependency-review:
    uses: doka-labs/workflows/.github/workflows/dependency-review.yml@<commit-sha> # v0.1.0
```

The check reported to GitHub is named `<caller job> / Review`, for example
`dependency-review / Review`. Required status checks in branch rulesets must
use that name.

## Release lines

A product repository describes its release lines, toolchain rules, scheduled
workloads and qualification gates in `.github/release-policy.json`;
`examples/release-policy.json` shows every field. The tools in
`tools/release_lines/` validate it strictly: exactly the documented fields, no
duplicate keys, a regular file of at most 1 MiB, and `repository` equal to the
repository of the run.

The toolchain section has two package lists. Packages in `frameworkPackages`
must belong to the line's framework major (EF Core of a major runs only on .NET
of that major); `majorCeilingPackages` only keeps Dependabot below the next
major, for packages a line may deliberately use at a lower major, such as a
security minimum of `System.Security.Cryptography.Pkcs` 9.0.21 on a .NET 10
line.

`.github/dependabot.yml` is generated from `.github/dependabot-template.yml`
(one entry per ecosystem and directory, without `target-branch`) and the active
lines: every entry once per active line, the SDK without major updates, and the
framework and ceiling packages below the line's next major. Generate it with:

```sh
python3 -E -s -m tools.release_lines render-dependabot --root <product-repository> > <product-repository>/.github/dependabot.yml
```

run from a checkout of this repository.

### `release-policy-check.yml`

Validates the policy, checks that the toolchain of the checkout fits its line,
and compares `.github/dependabot.yml` with the generated file; on a difference
the expected file is written to the job summary. On pull requests the line is
the base branch, which must be a registered line of the policy on `main`. Call
it from the repository's CI:

```yaml
jobs:
  release-policy:
    uses: doka-labs/workflows/.github/workflows/release-policy-check.yml@<commit-sha> # v0.2.0
```

### `release-lines-coordinator.yml`

GitHub runs schedules only on the default branch. A short scheduled workflow
on `main` calls the coordinator with one workload id from the policy. The
coordinator reads the policy from the current head of protected `main`,
dispatches the workload's workflow on every other active line at that line's
protected head, and verifies each dispatched run: identity, correlation title,
and jobs (every job in `requiredJobs` present and successful, matrix rows
matched as `<job> (...)`; every other job successful or skipped; no job
failed, cancelled or timed out). A failing line never hides the result of
another. The report is kept as an artifact.

```yaml
name: Release Lines

on:
  schedule:
    - cron: "35 4 * * *"
  workflow_dispatch:

permissions:
  contents: read

jobs:
  audit:
    permissions:
      contents: read
      actions: write
    uses: doka-labs/workflows/.github/workflows/release-lines-coordinator.yml@<commit-sha> # v0.2.0
    with:
      workload: audit
```

`timeoutMinutes` of a workload is capped at 330 because the coordinator waits
inside one hosted job, which GitHub ends after 6 hours.

### `release-line-guard.yml`

The first job of every workload workflow. A run dispatched by the coordinator
proceeds only on an active line other than `main`, at the exact commit the
coordinator resolved. A scheduled run on `main` proceeds only while `main` is an
active line. Pull requests, pushes and manual runs proceed unrestricted. The
workload workflow declares the dispatch inputs and the run name the
coordinator checks:

```yaml
name: Dependency Audit
run-name: ${{ inputs.maintenance && format('maintenance {0}', inputs.schedule-correlation) || 'Dependency Audit' }}

on:
  schedule:
    - cron: "35 4 * * *"
  workflow_dispatch:
    inputs:
      maintenance:
        type: boolean
        default: false
      schedule-correlation:
        type: string
        default: ""
      schedule-source-sha:
        type: string
        default: ""

permissions:
  contents: read

jobs:
  guard:
    uses: doka-labs/workflows/.github/workflows/release-line-guard.yml@<commit-sha> # v0.2.0
    with:
      maintenance: ${{ inputs.maintenance || false }}
      schedule-correlation: ${{ inputs.schedule-correlation || '' }}
      schedule-source-sha: ${{ inputs.schedule-source-sha || '' }}

  dependency-audit:
    needs: guard
    if: needs.guard.outputs.proceed == 'true'
    uses: doka-labs/workflows/.github/workflows/dependency-audit.yml@<commit-sha> # v0.2.0
```

### `coverage-gate.yml`

Merges the Cobertura reports of the run (line by line across test projects)
and enforces `.github/coverage-policy.json`: minimum line and branch percent
per assembly and per critical class, a branch floor of `null` for a
branch-free surface, optional maximum report age, no empty report, and a
threshold with executed lines for every package in `release.packages`.

```json
{
  "schemaVersion": 1,
  "evidenceMaxAgeSeconds": null,
  "assemblies": [{"name": "Doka.Example", "minimumLinePercent": 90, "minimumBranchPercent": 80}],
  "criticalClasses": []
}
```

## Releases

One `release-candidate.yml` per product repository, started by
`workflow_dispatch` on a release line with the version. It calls these
workflows, with the repository's own build, test and package jobs between
them:

1. `release-preflight.yml`: the run is on the current head of a line that may
   qualify or publish the version, and `CHANGELOG.md` has a dated section for
   it (`## [X.Y.Z] - YYYY-MM-DD` or `## X.Y.Z (YYYY-MM-DD)`). If a draft release
   with a candidate exists for the version, `resume` is `true` and the run
   continues with publication without repeating tests.
2. The repository's jobs build, test and pack. They upload the TRX results and
   one artifact with every `.nupkg`, its `.snupkg` and a `<PackageId>.spdx.json`.
3. `release-qualification.yml` (see above).
4. `release-candidate.yml`: checks package identity and source binding,
   restores each package in an isolated consumer from the candidate bytes and
   requires the restored archives to be byte-identical, generates one SPDX 2.2
   SBOM per package from that consumer with `sbom-tool` 4.1.5 and validates it,
   verifies each Portable PDB against its assembly (`tools/NuGetSymbols`),
   attests build provenance and the SBOMs, and stores the candidate as a draft
   release. The job summary shows the command to tag the commit.
5. The maintainer checks locally with
   `python3 -E -s -m tools.release_publication pre-tag --version <version>`
   (run from a checkout of this repository, in the product checkout or with
   `--root`): clean checkout on the current head of the protected line, tag and
   version unused, the draft's candidate built from this commit, SSH signing
   configured with a key listed in `.github/allowed_signers`. Then the
   maintainer creates and pushes the SSH-signed annotated tag `v<version>`.
6. `release-publish.yml` in the publisher environment: after approval it
   rechecks the assets against their attestations, the policy on `main`, the
   environment grants and the signed tag, attests the admission, pushes what
   nuget.org does not hold yet, reads every package back signed and every
   Portable PDB from the symbol server, restores every package from nuget.org
   only in an isolated consumer, then publishes the immutable release.
   If the readback is still pending after one hour, the job ends without
   failure and re-running it continues.

```yaml
name: Release Candidate

on:
  workflow_dispatch:
    inputs:
      version:
        type: string
        required: true
      qualification-only:
        type: boolean
        default: false

permissions:
  contents: read

jobs:
  preflight:
    permissions:
      contents: write
    uses: doka-labs/workflows/.github/workflows/release-preflight.yml@<commit-sha> # v0.3.0
    with:
      version: ${{ inputs.version }}
      qualification-only: ${{ inputs.qualification-only }}

  build:
    needs: preflight
    if: needs.preflight.outputs.resume == 'false'
    runs-on: ubuntu-26.04
    steps:
      - run: echo "Build, test, pack; upload test-results-* and the packages artifact."

  qualification:
    needs: [preflight, build]
    if: needs.preflight.outputs.resume == 'false'
    permissions:
      contents: read
      actions: read
      id-token: write
      attestations: write
    uses: doka-labs/workflows/.github/workflows/release-qualification.yml@<commit-sha> # v0.3.0
    with:
      version: ${{ inputs.version }}
      qualification-only: ${{ inputs.qualification-only }}
      test-results-pattern: test-results-*

  candidate:
    needs: [preflight, qualification]
    if: needs.preflight.outputs.resume == 'false' && !inputs.qualification-only
    permissions:
      contents: write
      actions: read
      id-token: write
      attestations: write
    uses: doka-labs/workflows/.github/workflows/release-candidate.yml@<commit-sha> # v0.3.0
    with:
      version: ${{ inputs.version }}
      packages-artifact: packages

  publish:
    needs: [preflight, candidate]
    if: ${{ !cancelled() && !inputs.qualification-only && (needs.candidate.result == 'success' || needs.preflight.outputs.resume == 'true') }}
    permissions:
      contents: write
      id-token: write
      attestations: write
    uses: doka-labs/workflows/.github/workflows/release-publish.yml@<commit-sha> # v0.3.0
    with:
      tag: ${{ needs.preflight.outputs.tag }}
    secrets:
      NUGET_USER: ${{ secrets.NUGET_USER }}
```

