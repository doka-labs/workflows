# Predicate `release-qualification-v1`

Predicate type:
`https://github.com/doka-labs/workflows/blob/main/docs/predicates/release-qualification-v1.md`

Signed by `doka-labs/workflows/.github/workflows/release-qualification.yml`.
Subject: the `git-tree-<tree>.txt` file of the qualified commit.

| Field | Type | Meaning |
| --- | --- | --- |
| `schemaVersion` | integer | `1` |
| `repository`, `sourceRef`, `commit`, `tree`, `version` | string | the candidate |
| `snapshot` | object | the `release-line-snapshot-v1` predicate |
| `gates` | array | each gate: `name`, `kind`, `runId`, `attempt`; protected-check gates add `workflow` (the PR workflow whose attestation holds the gate), `pullRequest`, `tree` (the fully qualified tree) and `documentationTrees` (the documentation-only trees between it and the candidate, newest first); candidate-produced gates add `jobs` and `conclusion` |
| `tests` | array | per test project: `assembly`, `project`, `passed`, `skipped`, `trx` (`file`, `sha256`) |
| `packageValidation` | array | per release package: `package` and the `project` that packs it with `EnablePackageValidation=true` |

This file is not changed after the first release of this repository that
contains it; a changed predicate gets a new file with a new version.
