# Predicate `release-admission-v1`

Predicate type:
`https://github.com/doka-labs/workflows/blob/main/docs/predicates/release-admission-v1.md`

Signed by `doka-labs/workflows/.github/workflows/release-publish.yml` after
the environment approval and all checks of the publish job, before the first
irreversible write (NuGet push, release publication).

Subject: the `candidate.json` manifest of the draft release.

| Field | Type | Meaning |
| --- | --- | --- |
| `schemaVersion` | integer | `1` |
| `repository`, `version`, `tag` | string | the candidate |
| `tagObject` | string | SHA of the signed annotated tag object verified locally and by GitHub |
| `commit`, `tree` | string | the qualified commit and its tree |
| `authorityCommit` | string | commit of `main` the release policy was read from |
| `runId`, `attempt` | integer | the publishing run attempt |

This file is not changed after the first release of this repository that
contains it; a changed predicate gets a new file with a new version.
