# Changelog

Versions follow Semantic Versioning: a major version changes a workflow input
or output, a tool's command line, or the schema of a policy file
(`.github/release-policy.json`, `.github/coverage-policy.json`). Callers pin a
commit SHA with the version as a comment; Dependabot updates both.

The first version is 0.1.0. A version is the SSH-signed tag `v<version>` on a
commit of `main` whose Self Check passed on GitHub; `release.yml` publishes it
as an immutable GitHub release with the notes of its section. Until then its
entries stay under Unreleased.

## [Unreleased]

## [0.2.0] - 2026-11-24

### Added

- `decision-records.yml`: holds `docs/decisions` to the Doka MADR Enterprise
  Profile 1.0 with `madr-check` 0.1.0, a container image pinned by digest
  that runs without network on a read-only checkout.

## [0.1.1] - 2026-11-24

### Added

- MIT license, the same as in the other Doka Labs repositories.

### Fixed

- The CI example in the README calls `release-pr-attestation.yml` with
  `always()`, so documentation-only pull requests are attested as the release
  qualification requires.
- `coverage-gate.yml` joins each Cobertura file name with the report's source
  root before merging. Reports of test projects that instrument a different
  set of assemblies name the same file differently, so the gate counted those
  lines twice and measured too little. A report with several source roots now
  fails, because its relative file names are ambiguous.

## [0.1.0] - 2026-11-23

### Added

- `dependency-audit.yml`: audit of every project file for known direct and
  transitive vulnerabilities.
- `dependency-review.yml`: dependency review with exact-version license
  exceptions.
- `workflow-lint.yml`: actionlint, zizmor and shellcheck.
- `coverage-gate.yml`: per-assembly and per-class coverage thresholds.
- `code-style.yml`: warning-level C# style and unnecessary usings.
- `package-check.yml`: candidate package checks for pull request packages.
- `pr-assign.yml`: assigns the author of a pull request, bots excluded.
- `change-scope.yml` and `required-gate.yml`: documentation-only pull
  requests skip the heavy jobs; the release qualification traces them back to
  a fully qualified tree.
- Release lines: `release-policy-check.yml`, `release-lines-coordinator.yml`,
  `release-line-guard.yml`.
- Qualification: `release-pr-attestation.yml`, `release-qualification.yml`.
- Releases: `release-preflight.yml`, `release-candidate.yml`,
  `release-publish.yml`, and the local `pre-tag` check.
- `self-check.yml`: lint and tool tests of this repository.
- `release.yml`: immutable release of this repository when its signed version
  tag is pushed.
