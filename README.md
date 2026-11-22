# doka-labs/workflows

Reusable GitHub Actions workflows shared by the doka-labs repositories. Each
workflow is maintained once here; repositories call it by commit SHA and keep
their own inputs.

## Calling a workflow

Pin the call to a full commit SHA and keep the version as a comment so
Dependabot can update it:

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
| `runs-on` | `ubuntu-24.04` | Runner label |
| `locked-mode` | `true` | Restore with `--locked-mode` |
| `retention-days` | `14` | Retention of the evidence artifact |

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

### `dependency-review.yml`

Reviews the dependency delta of a pull request with
`actions/dependency-review-action`. Fails on advisories at or above
`fail-on-severity` and on licenses outside `allow-licenses`. Packages listed in
`license-exceptions` are exempt from the license check only at their exact
reviewed versions; that check lives in `tools/dependency_review/`.

| Input | Default | Purpose |
| --- | --- | --- |
| `runs-on` | `ubuntu-24.04` | Runner label |
| `fail-on-severity` | `high` | Lowest failing advisory severity |
| `allow-licenses` | Apache-2.0, BSD-2-Clause, BSD-3-Clause, CC0-1.0, ISC, MIT, MIT-0, PostgreSQL, Unlicense | Allowed SPDX identifiers |
| `license-exceptions` | `pkg:nuget/Microsoft.Data.SqlClient.SNI.runtime@6.0.3`, `pkg:nuget/Microsoft.Identity.Client.NativeInterop@0.20.6` | `pkg:nuget/Name@version` entries, comma-separated |

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
