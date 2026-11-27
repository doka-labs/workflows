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

## Releasing this repository

A version of this repository is released in three steps:

1. Move the entries under `## [Unreleased]` in `CHANGELOG.md` into a dated
   section `## [<version>] - <YYYY-MM-DD>` and merge it to `main`.
2. Wait for the Self Check of the merge commit, then tag that commit with an
   SSH-signed annotated tag and push it:
   `git tag -s v<version> <commit> -m "v<version>"` and
   `git push origin v<version>`. Only administrators can create tags.
3. `release.yml` runs on the tag. It requires GitHub to verify the tag's
   signature, the tagged commit to lie on `main` and its Self Check to have
   passed (it waits up to 20 minutes for a running one), takes the notes from
   the version's `CHANGELOG.md` section and publishes the immutable release.
   It then waits for GitHub's release attestation and checks it with
   `gh release verify`. A rerun of the same tag only verifies the published
   release.

Immutable releases are enforced by the organization. The job token cannot read
that setting, so a release that does not become immutable makes the run fail.

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
Use a Linux x64 runner for the pinned actionlint binary and zizmor wheel.

```yaml
jobs:
  workflow-lint:
    uses: doka-labs/workflows/.github/workflows/workflow-lint.yml@<commit-sha> # v0.1.0
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
framework and ceiling packages below the line's next major. A pull request that
changes the lines needs no local tool: `release-policy-check.yml` fails while
the file differs and writes the expected file to its job summary, to be
committed as it is. The same output comes from

```sh
python3 -E -s -m tools.release_lines render-dependabot --root <product-repository> > <product-repository>/.github/dependabot.yml
```

in a checkout of this repository at the version `main` of the product
repository pins.

### `release-policy-check.yml`

Validates the policy, checks that the toolchain of the checkout fits its line,
and compares `.github/dependabot.yml` with the generated file; on a difference
the expected file is written to the job summary. On pull requests the line is
the base branch, which must be a registered line of the policy on `main`. Call
it from the repository's CI:

```yaml
jobs:
  release-policy:
    uses: doka-labs/workflows/.github/workflows/release-policy-check.yml@<commit-sha> # v0.1.0
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

Child workflows run concurrently. The coordinator serializes its REST calls,
caps its own requests at 480 per rolling hour, and honors GitHub's backoff
headers. See [GitHub's REST API limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api).

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
    uses: doka-labs/workflows/.github/workflows/release-lines-coordinator.yml@<commit-sha> # v0.1.0
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
    uses: doka-labs/workflows/.github/workflows/release-line-guard.yml@<commit-sha> # v0.1.0
    with:
      maintenance: ${{ inputs.maintenance || false }}
      schedule-correlation: ${{ inputs.schedule-correlation || '' }}
      schedule-source-sha: ${{ inputs.schedule-source-sha || '' }}

  dependency-audit:
    needs: guard
    if: needs.guard.outputs.proceed == 'true'
    uses: doka-labs/workflows/.github/workflows/dependency-audit.yml@<commit-sha> # v0.1.0
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

The gate reads every file named `*.cobertura.xml`, or with a numeric
timestamp before `.xml` as coverlet.MTP writes it, in the artifacts that match
`reports-pattern`. The coverage tools of the Doka repositories write
different reports, so the thresholds and class names of a policy hold for one
tool only:

| Tool | Runner | Report file name, measured | Nested class | Branches of the probe |
| --- | --- | --- | --- | --- |
| coverlet.collector | VSTest | `coverage.cobertura.xml` | `Probe.Lib.Calc/Nested` | 4/8 |
| coverlet.MTP | Microsoft Testing Platform | `coverage.cobertura.261126103325198.xml`, with `--coverlet-file-prefix integration-mariadb` `integration-mariadb.coverage.cobertura.261126132205055.xml` | `Probe.Lib.Calc/Nested` | 4/8 |
| Microsoft.CodeCoverage (`Format=cobertura`) | VSTest | `runner_host_2026-11-26.11_33_22.cobertura.xml` | `Probe.Lib.Calc.Nested` | 2/6 |

- A critical class is matched by its exact name and counts only its own
  lines. Nested classes and async methods are separate classes in the report,
  for example `Probe.Lib.Calc/<LaterAsync>d__2`, and need their own entry.
- A change of the coverage tool can change the measured branches. Check the
  thresholds against a report of the new tool in the same pull request.
- Coverage resolves file names against their report source roots and maps
  files below the checkout to repository-relative paths. For reports from
  other workspaces, set `source-checkout-roots` to the producer checkout
  paths, one per line. Unknown absolute paths retain their full identity.

The reports under `tools/coverage/tests/data` show each tool's output for the
same probe library, `Probe.Lib`, measured with the versions listed there.

## Pull requests

### `code-style.yml`

Restores the solution (`--locked-mode` unless `locked-mode` is `false`) and
runs the two checks MySql, SafeMigrations and NestedSet run today, without
changing files: `dotnet format <solution> style --severity warn
--verify-no-changes --no-restore`, and the same with `--diagnostics IDE0005
--severity hidden` for unnecessary usings. Whitespace is not checked.

### `package-check.yml`

Checks the packages of a pull request with the candidate's checks, without
writing anything: package identity and source binding, exactly the packages
of `release.packages`, Portable PDBs against their assemblies, the exact pin
between packages that share internals, an isolated consumer per package that
must restore the exact bytes, and a validated SBOM per package. The caller's build job uploads every `.nupkg` and `.snupkg` as
one artifact and passes the packed version.
Use a Linux x64 runner for the pinned SBOM tool. The symbol check runs with
the SDK selected by the product's `global.json`.

### `pr-assign.yml`

Assigns the author of a new or reopened pull request, so lists show who
drives it. Bots such as Dependabot are left out, and so are pull requests from
forks, whose token is read-only. An author GitHub reports as ineligible gets
a notice. API and transport failures fail the job.

```yaml
name: Pull Request

on:
  pull_request:
    types:
      - opened
      - reopened

permissions: {}

jobs:
  assign:
    permissions:
      pull-requests: write
    uses: doka-labs/workflows/.github/workflows/pr-assign.yml@<commit-sha> # v0.1.0
```

### `decision-records.yml`

Holds `docs/decisions` to the Doka MADR Enterprise Profile 1.0 with
`madr-check` 0.1.1, run as the container image
`ghcr.io/kdominic89/madr-check` pinned by digest, without network and on a
read-only mount of the checkout: `validate --profile doka` checks every
record, and `index check --profile doka` fails on a stale or partial generated
index; a directory without an index passes. The profile is fixed, so a
repository cannot leave it by changing the keys of its records. The job needs
`contents: read` only. Decision records are documentation, so the caller runs
the job for `docs` pull requests too and does not list it as skippable in
`required-gate.yml`.

The digest sits in a `run` block, which Dependabot does not read; a new
`madr-check` version is pinned in this workflow by hand and released as a new
version of this repository.

```yaml
jobs:
  decision-records:
    uses: doka-labs/workflows/.github/workflows/decision-records.yml@<commit-sha> # v0.2.0
```

### `architecture-document.yml`

Holds `docs/architecture.md` to the code with `as-built-dotnet` 0.1.0, run as
the container image `ghcr.io/kdominic89/as-built-dotnet` pinned by digest,
without network. The job restores `solution` on the runner (`--locked-mode`
unless `locked-mode` is `false`), because the checker tells a call into a
package from any other name only from the restored assets. It then mounts the
checkout and the NuGet package folder read-only at the paths the restore
wrote and runs `check docs/architecture.md`.

The check fails when a diagram or table the document carries and the checker
owns differs from the code, when an item `` `A` does not use `B` `` does not
hold, on a broken link, on a diagram without `accTitle` and `accDescr`, and on
an absolute machine path or a non-ASCII character outside diagrams and
tables. A repository without `docs/architecture.md` fails. The checker owns a
Mermaid block by its `accTitle` (`System context`, `Package dependencies`,
`Areas of <project>`, `Verification`, `Reach of <Type.Member>`) and a table by
its header (`| Area | Uses |`, `| Area | Decisions |`) under a heading that
names a project; it adds no block and leaves every other line as it is.
Locally, the same image with `write` instead of `check` and a writable mount
rewrites the owned blocks.

The job needs `contents: read` only. The document describes the code, so the
caller runs the job for `docs` pull requests too and does not list it as
skippable in `required-gate.yml`. The digest sits in a `run` block, which
Dependabot does not read; a new checker version is pinned in this workflow by
hand and released as a new version of this repository.

```yaml
jobs:
  architecture-document:
    uses: doka-labs/workflows/.github/workflows/architecture-document.yml@<commit-sha> # v0.3.0
    with:
      solution: Doka.Example.slnx
```

### Documentation-only changes

`change-scope.yml` classifies a pull request by `git diff` between its base
and the tested merge commit. `.github/change-scope.json` is the repository's
contract: the paths it lists are declared independent of every build, test and
package. A change is `docs` only when every changed path is declared there and
the checks below find no dependency; anything unknown, every other change and
every other event is `full`. Static checks cannot prove that nothing reads a
file, because a task or a test can open any path; they find the dependencies a
repository writes down. Never documentation, whatever the policy lists:

- `.github/**` (the policy itself included), `global.json`, source, MSBuild
  (`.csproj`, `.fsproj`, `.vbproj`, `.sqlproj`, `.props`, `.targets`, `.proj`,
  `.projitems`, `.shproj`), analyzer and signing configuration
  (`.editorconfig`, `.globalconfig`, `.ruleset`, `.snk`, `.rsp`, `.resx`),
  JSON, YAML, Python and shell files, and tracked symbolic links;
- everything below a project directory, because SDK projects include the files
  of their directory by default;
- every path an MSBuild item, property or task parameter names, resolved from
  its own file and from every project, also through symbolic links and MSBuild
  escapes; a file name built from a property the repository sets matches its
  values, or every file when a script or workflow sets it;
- every declared path that a tracked non-documentation file names: its path,
  its file name, or its stem or a directory as a quoted or separated path part
  (`Path.Combine(root, "docs")`, `root / "docs"`, `<DocDir>docs</DocDir>`).
  Markdown files and URLs are not searched.

A miss cannot publish anything untested: every release run runs every gate
again, and a CI run on a push to `main`, as in the example below, is always
`full`, so a broken build shows up right after the merge. In MySql, SafeMigrations
and NestedSet today, contract tests and release tools read `docs/`, so only
files such as `CODE_OF_CONDUCT.md` or `CONTRIBUTING.md` stay `docs`.

```json
{
  "schemaVersion": 1,
  "documentation": ["docs/**", "CHANGELOG.md", "CONTRIBUTING.md"]
}
```

A required check whose workflow is skipped by a path filter stays pending and
blocks the pull request, so the CI workflow always starts; its heavy jobs are
skipped for `docs` and `required-gate.yml` is the required check. The gate
passes when every needed job succeeded, or was skipped for a `docs` change and
is listed in `skippable`. A repository whose tests read documentation (MySql's
link and ADR contract tests) keeps those jobs out of `skippable`.

```yaml
name: CI

on:
  pull_request:
  push:
    branches:
      - main

permissions:
  contents: read

jobs:
  scope:
    uses: doka-labs/workflows/.github/workflows/change-scope.yml@<commit-sha> # v0.1.0

  style:
    needs: scope
    if: needs.scope.outputs.scope == 'full'
    uses: doka-labs/workflows/.github/workflows/code-style.yml@<commit-sha> # v0.1.0
    with:
      solution: Doka.Example.slnx

  build:
    needs: scope
    if: needs.scope.outputs.scope == 'full'
    runs-on: ubuntu-26.04
    outputs:
      version: ${{ steps.pack.outputs.version }}
    steps:
      - id: pack
        run: echo "The repository's restore, test and pack steps upload the packages artifact."

  packages:
    needs: [scope, build]
    if: needs.scope.outputs.scope == 'full'
    permissions:
      contents: read
      actions: read
    uses: doka-labs/workflows/.github/workflows/package-check.yml@<commit-sha> # v0.1.0
    with:
      version: ${{ needs.build.outputs.version }}
      packages-artifact: packages

  required:
    needs: [scope, style, build, packages]
    if: always()
    uses: doka-labs/workflows/.github/workflows/required-gate.yml@<commit-sha> # v0.1.0
    with:
      results: ${{ toJSON(needs) }}
      scope: ${{ needs.scope.outputs.scope }}
      skippable: '["style", "build", "packages"]'

  attest:
    needs: required
    # WHY: Without always(), the jobs skipped for a docs change would skip this job too.
    if: always() && github.event_name == 'pull_request' && needs.required.result == 'success'
    permissions:
      contents: read
      actions: read
      id-token: write
      attestations: write
    uses: doka-labs/workflows/.github/workflows/release-pr-attestation.yml@<commit-sha> # v0.1.0
```

The required check is then `required / Required`. `release-pr-attestation.yml`
runs after the gate on every pull request, also a `docs` one, and attests the
scope with the tree of the base commit. The release qualification
accepts a `docs` attestation only by walking back through those base trees to
a `full` one of the same workflow and line, recomputing every step with
`git diff` and the candidate's policy; the release run itself runs every gate
again.

## Releases

Before a release, the maintainer checks the release line on their own machine
with `eng/pre-tag-check.sh VERSION` in the product repository, a copy of
`examples/pre-tag-check.sh`. The script reads the one `doka-labs/workflows`
commit that the line's `release-candidate.yml` pins, fetches exactly that
commit into a temporary directory and runs `python3 -m tools.release_publication
pre-tag` there, so each line checks with its own version and no checkout of
this repository is needed. It needs `git`, a signed-in `gh` and `python3`,
writes nothing, and reports one line `OK` or `FAIL` with a remedy per check:
the changelog section, the origin repository, immutable releases, a clean
checkout on the current head of the line, the tag and the version unused, no
other version tag on the commit, the pull request qualification of the
commit's tree, and the signing setup (`gpg.format` ssh, `user.signingkey`, the
key in `.github/allowed_signers` of the commit and in
`gpg.ssh.allowedSignersFile` for `git tag -v`). A check whose input failed
says what it waits for.

The release itself is one `release-candidate.yml` per product repository,
started by `workflow_dispatch` on a release line with the version. It calls
these workflows, with the repository's own build, test and package jobs
between them:

1. `release-preflight.yml`: the run is on the current head of a line that may
   qualify or publish the version, and `CHANGELOG.md` has a dated section for
   it (`## [X.Y.Z] - YYYY-MM-DD`, the only accepted form). If a draft release
   with a candidate exists for the version, the run resumes with publication
   without repeating tests, but only when the draft passes the publish job's own
   verification: every asset uploaded unchanged, the provenance and exactly the
   staged SBOMs attested, and the release qualification. Otherwise it stops
   and names what is missing.
2. The repository's jobs build, test and pack. They upload the TRX results and
   one artifact with every `.nupkg` and its `.snupkg`; the candidate generates
   the SBOMs itself.
3. `release-qualification.yml`: the line and version fit the release policy,
   the commit's tree carries a pull request qualification, the candidate's
   own gate jobs succeeded and the TRX results of every test project are
   complete. Every release package must come from a project that sets
   `EnablePackageValidation` in the pack configuration (`pack-configuration`,
   default `Release`). The job builds those projects, installs
   `Microsoft.DotNet.ApiCompat.Tool` of the checkout's SDK version from its
   signature-verified nuget.org archive and validates the exact archives of
   the `packages-artifact` (default `packages`) with the settings the SDK's
   pack would use: suppression files, `NoWarn`, strict modes, rules, the
   references of each target framework unless
   `RunPackageValidationWithoutReferences` is set, and the baseline of
   `PackageValidationBaselineVersion`, downloaded and verified from
   nuget.org. A `PackageValidationBaselinePath` fails the qualification,
   because a local file cannot be proven. The qualification records each validated archive with its
   SHA-256; a new candidate must match them, and candidates qualified before
   0.6.0 stay publishable.
4. `release-candidate.yml`: checks package identity and source binding,
   restores each package in an isolated consumer from the candidate bytes and
   requires the restored archives to be byte-identical, generates one SPDX 2.2
   SBOM per package from that consumer with `sbom-tool` 4.1.5 and validates it,
   verifies each Portable PDB against its assembly (`tools/NuGetSymbols`),
   attests build provenance and the SBOMs, and stores the candidate as a draft
   release. It requires an exact pin `[<version>]` wherever a package depends
   on another package of the candidate whose assemblies grant it their
   internals (`InternalsVisibleTo`).
5. The `Tag check` job of `release-candidate.yml`, after the SBOM jobs, checks
   in the same run, and so with the `doka-labs/workflows` version the release
   line pins: the changelog section, the tag and the version still unused, no
   other version tag on the commit, the draft holding this run's candidate and
   passing the publish job's verification, and a key in
   `.github/allowed_signers` of the commit. The draft exists only in this run,
   and the other checks are repeated because the line or nuget.org can change
   after the local check. Its summary lists one line `OK` or `FAIL` per check.
   When all pass and the publish job waits for approval, the maintainer
   creates the SSH-signed annotated tag `v<version>` on the candidate commit in
   their checkout of the line, verifies it with `git tag -v` and pushes only
   that tag. Until the push the tag is local and can be deleted; a pushed tag
   blocks a new candidate for its version, so a broken candidate is deleted
   before tagging, not after.
6. The `publish` job of the repository's release workflow runs in the
   publisher environment and calls the composite action
   `.github/actions/release-publish` of this repository. After approval it
   rechecks the assets against their attestations, the policy on `main`, the
   reviewers and branch grants of `release.environment`, that the run comes
   from `release.workflowPath`, and the signed tag, and attests the admission.
   The tools request no OIDC token of their own; nuget.org compares the token
   of `NuGet/login` with the Trusted Publishing policy. Before
   pushing, it checks the existing Latest release and verifies the current
   draft assets. It pushes what nuget.org does not hold yet, reads every
   package back signed and every Portable PDB from the symbol server,
   restores every package from nuget.org only in an isolated consumer,
   compares the current draft with the candidate
   once more, then publishes the immutable release and requires its whole asset
   inventory to equal the candidate.
   If the readback is still pending after one hour, the job ends without
   failure and re-running it continues.

The publish job is an action called from the product repository's own job, not
a reusable workflow, because nuget.org Trusted Publishing compares the token's
`job_workflow_ref` with the policy's workflow file in the product repository; a
job of a reusable workflow from `doka-labs/workflows` names this repository
there and is refused. An action keeps the calling job's token. The calling job
sets `environment`, the permissions `contents: write`, `actions: read`,
`id-token: write` and `attestations: write`, one queue per repository, and the
timeout, as in the example below. The release admission is therefore signed by
the product repository's release workflow. Configure the Trusted Publishing
policy on nuget.org with the repository owner, the repository, the workflow
file `release-candidate.yml` (the file name of `release.workflowPath`) and the
environment `nuget`, and store the nuget.org profile name as the environment
secret `NUGET_USER`.

Run the candidate workflow on GitHub-hosted Linux x64 for the pinned SBOM
tool and verifiable attestations. Release qualification and PR attestation
require GitHub-hosted Linux, and publication requires a GitHub-hosted runner.

The job token cannot read whether immutable releases are enabled: the setting
needs the Administration permission, which a `GITHUB_TOKEN` never has. Enforce
it for the product repository in the organization settings; `pre-tag` reads it
with the maintainer's credentials, and the publish job fails when the
published release is not immutable.

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
      attestations: read
    uses: doka-labs/workflows/.github/workflows/release-preflight.yml@<commit-sha> # v0.1.0
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
    uses: doka-labs/workflows/.github/workflows/release-qualification.yml@<commit-sha> # v0.1.0
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
    uses: doka-labs/workflows/.github/workflows/release-candidate.yml@<commit-sha> # v0.1.0
    with:
      version: ${{ inputs.version }}
      packages-artifact: packages

  publish:
    needs: [preflight, candidate]
    if: ${{ !cancelled() && !inputs.qualification-only && (needs.candidate.result == 'success' || needs.preflight.outputs.resume == 'true') }}
    # WHY: contents write lists the draft and publishes the release; id-token signs the admission and obtains the
    # NuGet credential through Trusted Publishing; attestations write stores the admission. actions read is the
    # repository permission of "GET /repos/{owner}/{repo}/environments/{environment_name}" and of its deployment
    # branch policies, which the environment check reads.
    # https://learn.microsoft.com/nuget/nuget-org/trusted-publishing
    # https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps
    permissions:
      contents: write
      actions: read
      id-token: write
      attestations: write
    # WHY: The environment is release.environment of the policy and of the nuget.org policy; its reviewers approve
    # the publication, and the action requires it in the job's environment claim.
    environment: nuget
    # WHY: Publications of one repository run one after another and are never cancelled midway; queue: max keeps
    # up to 100 waiting. https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idconcurrency
    concurrency:
      group: ${{ github.repository }}-publication
      cancel-in-progress: false
      queue: max
    # WHY: The newest generally available GitHub-hosted Ubuntu image.
    # https://github.com/actions/runner-images/issues/14747
    runs-on: ubuntu-26.04
    # WHY: Successful publish jobs took up to 60.1 minutes (NestedSet) and 46.6 minutes (MySql) in 31 runs; the NuGet
    # readback alone may wait one hour. 180 minutes is NestedSet's limit.
    timeout-minutes: 180
    steps:
      - uses: doka-labs/workflows/.github/actions/release-publish@<commit-sha> # v0.5.0
        with:
          tag: ${{ needs.preflight.outputs.tag }}
          nuget-user: ${{ secrets.NUGET_USER }}

  # WHY: A resumed run skips candidate, and a skip passes to every job that needs it unless the job has its own
  # condition. https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idneeds
  api-reference:
    needs: [preflight, publish]
    if: ${{ !cancelled() && needs.publish.result == 'success' }}
    permissions:
      contents: read
      pages: write
      id-token: write
    uses: doka-labs/workflows/.github/workflows/api-reference.yml@<commit-sha> # v0.3.0
    with:
      tag: ${{ needs.preflight.outputs.tag }}
```

### `api-reference.yml`

Runs after the publication and builds a DocFX 2.81.0 site from the released
tag: the API reference of every package in `release.packages` of the release
policy and every Markdown file under `docs/`. The project of a package is the
packable project under `src/` whose MSBuild-evaluated `PackageId` is the package
ID. Selection, restore and DocFX use the tag's version and the
`pack-configuration` that built the packages, `Release` by default.
The tool writes the site sources into `.docfx/` of the checkout: `docfx.json`,
`toc.yml`, `README.md` as `index.md`, and a copy of `docs/`; a repository that
has `.docfx/` fails, and nothing else of the checkout changes. DocFX builds
with warnings as errors, so a missing XML comment or a broken link fails the
run.

Documents keep relative links to any file of the repository, which the
checkers read. In the copy, a link to a repository file the site does not
hold (source, tests, samples, `LICENSE`) becomes a link to that file on GitHub
at the released tag, `blob/<tag>/<path>` or `tree/<tag>/<path>` for a
directory, as DocFX projects link source. A link to a document or image under
`docs/` stays relative, a link to `README.md` points to `index.md`, and a link
to a path the repository does not have stays as written, so DocFX reports it.
Code blocks and code spans are not changed. A link that leaves the
repository, an image outside `docs/`, and an embedded file (`[!code...]`,
`[!INCLUDE...]`) fail.

The navigation follows `docs/README.md`. Each document kind is a section
`## Tutorials`, `## How-to guides`, `## Reference` or `## Explanation`, in any
subset and in this order on the site; each section is a table whose header is
`| Document | Purpose |` and whose rows are `| [Title](path.md) | purpose |`
with a relative link that stays under `docs/`:

```markdown
## Tutorials

| Document | Purpose |
| --- | --- |
| [Getting started](guides/getting-started.md) | First import and export |
```

A repository with decision records in `docs/decisions` links
`decisions/README.md` exactly once, in the Explanation table. Before the
build, `madr-check` 0.1.1, the same image as in `decision-records.yml`, writes
`index.md` and `index.json` into the checkout with `index write --profile
doka`; it fails when a record does not satisfy the profile. The site lists
below that row first the index, then each record from `index.json` as
`<id> <title>`, sorted by id, with `(superseded)`, `(rejected)` or
`(deprecated)` appended to a record that is not binding or no longer
recommended. The tool reads only `schema_version`, `id`,
`title`, `status` and `path`, and fails on a `schema_version` other than 1.

The site on GitHub Pages shows the highest published stable version, and
before the first stable version the highest pre-release: a release replaces
it only when no published release has a higher version, so a maintenance
release of an older line keeps it. Nothing is deployed while the tag's release
is not a published, immutable release, as after a publish job whose NuGet
readback is still pending; re-running that job re-runs this workflow. The
site is built for every release either way. The deploy job checks eligibility
again while holding the shared Pages deployment lock, and the build checks out
`refs/tags/<tag>`, never a branch of the same name. Use a Linux x64 runner for
the pinned DocFX archive.
Set the repository's Pages source to GitHub Actions before the first release.
The `github-pages` environment deploys only from the branches it allows, so
it allows every release line whose release can replace the site.

## Community and support

The organization-wide policies apply to this repository:

- [Support channels and safe diagnostic sharing](https://github.com/doka-labs/.github/blob/main/SUPPORT.md)
- [Contributing](https://github.com/doka-labs/.github/blob/main/CONTRIBUTING.md)
- [Code of Conduct](https://github.com/doka-labs/.github/blob/main/CODE_OF_CONDUCT.md)
- [Security policy and private vulnerability reporting](https://github.com/doka-labs/.github/blob/main/SECURITY.md)
- [Governance and responsibilities](https://github.com/doka-labs/.github/blob/main/GOVERNANCE.md)

## License

The workflows and tools are MIT-licensed. See [LICENSE](LICENSE). The
[Code of Conduct](https://github.com/doka-labs/.github/blob/main/CODE_OF_CONDUCT.md#attribution)
is separately licensed under
[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).
