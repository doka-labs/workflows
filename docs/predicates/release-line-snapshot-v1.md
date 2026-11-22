# Predicate `release-line-snapshot-v1`

Predicate type:
`https://github.com/doka-labs/workflows/blob/main/docs/predicates/release-line-snapshot-v1.md`

Signed by `doka-labs/workflows/.github/workflows/release-qualification.yml`.
Subject: the `git-tree-<tree>.txt` file of the qualified commit.

The release line of the candidate as the release policy on `main` defined it
during qualification:

| Field | Type | Meaning |
| --- | --- | --- |
| `schemaVersion` | integer | `1` |
| `repository` | string | `owner/name` |
| `sourceRef`, `role`, `packageMajor`, `upstreamStage` | | the line |
| `channel` | string | `stable` or `prerelease`, from the candidate version |
| `stateAtQualification` | string | `prepared` or `active` |
| `policyRevision` | string | `revision` of the policy |
| `policyDigest` | string | SHA-256 of the canonical policy JSON |
| `policyCommit` | string | commit of `main` the policy was read from |
| `qualificationOnly` | boolean | `true` never permits publication; always `true` for a prepared line |

This file is not changed after the first release of this repository that
contains it; a changed predicate gets a new file with a new version.
