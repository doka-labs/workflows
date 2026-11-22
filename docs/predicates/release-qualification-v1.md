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
| `gates` | array | each gate: `name`, `kind`, `runId`, `attempt`, and `pullRequest` for protected-check gates |
| `tests` | array | per test project: `assembly`, `project`, `passed`, `skipped`, `trx` (`file`, `sha256`) |

This file is not changed after the first release of this repository that
contains it; a changed predicate gets a new file with a new version.
