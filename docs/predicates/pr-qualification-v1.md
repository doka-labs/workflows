# Predicate `pr-qualification-v1`

Predicate type:
`https://github.com/doka-labs/workflows/blob/main/docs/predicates/pr-qualification-v1.md`

Signed by `doka-labs/workflows/.github/workflows/release-pr-attestation.yml`
after every protected-check gate job of the pull request run succeeded.

Subject: a file `git-tree-<tree>.txt` whose content is the 40-character Git
tree ID of the tested merge commit followed by a newline.

| Field | Type | Meaning |
| --- | --- | --- |
| `schemaVersion` | integer | `1` |
| `repository` | string | `owner/name` of the product repository |
| `pullRequest` | integer | pull request number |
| `baseRef` | string | `refs/heads/<base branch>`, a registered release line |
| `baseSha` | string | base commit of the pull request |
| `baseTree` | string | tree ID of `baseSha` |
| `headSha` | string | head commit of the pull request |
| `mergeCommit` | string | merge commit the run tested (`GITHUB_SHA`) |
| `tree` | string | tree ID of `mergeCommit`; equals the subject |
| `workflow` | string | path of the calling CI workflow |
| `scope` | string | `docs` when `git diff` from `baseSha` to `mergeCommit` changes only documentation by the repository's `.github/change-scope.json`, otherwise `full`; computed by the attesting workflow |
| `gates` | array | `{name, jobs, conclusion}` of each gate job, `conclusion` is `success` |

This file is not changed after the first release of this repository that
contains it; a changed predicate gets a new file with a new version.
