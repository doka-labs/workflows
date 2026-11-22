# Changelog

Versions follow Semantic Versioning: a major version changes a workflow input
or output, a tool's command line, or the schema of a policy file
(`.github/release-policy.json`, `.github/coverage-policy.json`). Callers pin a
commit SHA with the version as a comment; Dependabot updates both.

## [Unreleased]

### Added

- `dependency-audit.yml`: audit of every project file for known direct and
  transitive vulnerabilities.
- `dependency-review.yml`: dependency review with exact-version license
  exceptions.
- `workflow-lint.yml`: actionlint, zizmor and shellcheck.
- `coverage-gate.yml`: per-assembly and per-class coverage thresholds.
- Release lines: `release-policy-check.yml`, `release-lines-coordinator.yml`,
  `release-line-guard.yml`.
- Qualification: `release-pr-attestation.yml`, `release-qualification.yml`.
- Releases: `release-preflight.yml`, `release-candidate.yml`,
  `release-publish.yml`, and the local `pre-tag` check.
- `self-check.yml`: lint and tool tests of this repository.
