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

## [0.6.1] - 2026-11-27

### Fixed

- The publish action pushed every symbol package on every run, also when
  resuming at a published release; nuget.org accepts such a repeated symbol
  package and replaces the available one. It now asks the symbol server for
  each Portable PDB first and pushes only the symbol packages whose PDBs are
  not served, as MySql does. Only a 404 counts as not served; other bytes for
  any PDB or an unclear answer such as 429 or 503 stop the run before a key is
  requested.
- `NuGet/login` and the push run only when a package or a symbol package is
  missing, so a run with nothing to publish requests no key.
- Authority, environment and the signed tag are checked again before the key
  is requested and before every single push, not once before all pushes.
- Tag verification, the `Tag check` of `release-candidate.yml` and
  `pre-tag-check` read `.github/allowed_signers` from the current head of
  `main` instead of the candidate commit. A key removed there no longer
  counts for any tag, also when a run resumes days later.

### Changed

- The README has a table of contents, task headings in the infinitive and
  `# v<version>` in its example pins instead of outdated versions. It is the
  authoritative description of each workflow; `docs/overview.md` says so,
  shows how the workflows work together, gives every diagram `accTitle` and
  `accDescr`, drops the numbers from its headings and corrects two outdated
  statements (product repositories do call the workflows; the release
  admission is signed by the product's release workflow).

## [0.6.0] - 2026-11-27

### Added

- `release-qualification.yml` validates the exact release archives with
  `Microsoft.DotNet.ApiCompat.Tool` of the checkout's SDK version, with the
  settings the pack would use and the baseline from nuget.org, and records
  each archive with its SHA-256. New optional inputs: `packages-artifact`
  (default `packages`) and `pack-configuration` (default `Release`). A new
  candidate must match the recorded archives; candidates qualified before
  stay publishable.

### Fixed

- The qualification reads `EnablePackageValidation` in the pack
  configuration and version instead of the project's default configuration.

- `api-reference.yml` deploys the site only when the tag's release is
  published and immutable. A publish job whose NuGet readback is still
  pending ends without failing while the release stays a draft; the site
  of such a version was deployed before. Re-running the publish job also
  re-runs the workflow, which then deploys.
- A stable release replaces the site only when no published release has a
  higher version, so a maintenance release of an older line no longer
  replaces the API of a newer one; before the first stable version the
  highest pre-release is shown. The release list is read page by page.
- The caller example gives the `api-reference` job the condition
  `!cancelled() && needs.publish.result == 'success'`. Without it the job
  was skipped in Tabular's resumed run 45908860276, because the skipped
  `candidate` job passed its skip on through `publish`.
- The reason given for pushing every symbol package on every run states
  what nuget.org does: it refuses a symbol package with 409 only while one
  of that version validates, and otherwise replaces the available one with
  the same bytes.
- `change-scope.yml`, `release-pr-attestation.yml`, the tag check of
  `release-candidate.yml` and `release.yml` configure the GitHub CLI
  credential helper in the step that reads with Git and give that step
  `GH_TOKEN`. Before, the helper was configured in a step of its own while
  the reading step had no token, or the reading step had a token and no
  helper.
- `api-reference.yml` checks out `refs/tags/<tag>`, so a branch of the same
  name can no longer replace the released source. Its deploy job checks
  again under the Pages lock whether the release replaces the site, and
  waiting deployments queue instead of replacing each other.
- `api-reference.yml` selects the project of a package by its
  MSBuild-evaluated `PackageId` and `IsPackable`, so a `PackageId` from
  `Directory.Build.props` is found. Selection, restore and the DocFX
  metadata use the pack's configuration and the tag's version; the new
  optional input `pack-configuration` defaults to `Release`.
- The symbol check of `release-candidate.yml` and `package-check.yml` and
  the NuGet push of `release-publish` run from the product checkout, so its
  `global.json` selects the SDK.
- The coverage gate joins a report's file names with its source root and
  removes only an explicit checkout prefix; two files of the same name in
  different directories are no longer merged. The new optional input
  `source-checkout-roots` names checkouts of other report jobs.
- `pr-assign.yml` treats only HTTP 404 as an author who cannot be assigned;
  other API and transport failures fail the job.
- Jobs whose pinned tools need Linux x64, or whose attestations must come from
  a GitHub-hosted runner, fail with a clear message on any other runner.

## [0.5.1] - 2026-11-26

### Fixed

- The publish job no longer requests an OIDC token of its own to read its
  environment and defining workflow. That request returned HTTP 400 in the
  first real publication (Tabular run 45886213179) and stopped it before any
  write. As in MySql, SafeMigrations and NestedSet, only `NuGet/login` and
  `actions/attest` request the token, and nuget.org compares its workflow
  file and environment with the Trusted Publishing policy; the tools still
  check the run's release workflow and the environment's reviewers and branch
  grants.

## [0.5.0] - 2026-11-26

### Added

- The candidate and `package-check.yml` require an exact pin `[<version>]`
  wherever a package depends on another package of the candidate whose
  assemblies grant it their internals.
- `release-candidate.yml` has a `Tag check` job after the SBOM jobs. It checks
  in the release line's own run that the draft holds this run's candidate and
  passes the publish verification, and repeats the checks before tagging.
- `examples/pre-tag-check.sh`, copied to `eng/pre-tag-check.sh` of a product
  repository, runs `pre-tag` with this repository at the commit the line's
  release workflow pins, without a checkout of this repository.

### Changed

- `pre-tag` runs before the candidate starts and checks the line, the version,
  the pull request qualification and the signing setup, including
  `gpg.ssh.allowedSignersFile` for `git tag -v`; the draft checks moved to
  the `Tag check` job, and it no longer prints a tag command.
- The `coverage-gate.yml` section of the README names how coverlet.collector,
  coverlet.MTP and Microsoft.CodeCoverage differ in report file names, nested
  class names and measured branches.
- The coverage gate also reads coverlet.MTP reports under the name the tool
  writes, `coverage.cobertura.<timestamp>.xml` with an optional prefix, so a
  caller no longer has to rename them.

### Fixed

- The qualification reads the skip reason of TRX files from the Microsoft
  Testing Platform (`--report-xunit-trx`, `Output/StdOut`) as well as from
  VSTest (`--logger trx`, `Output/ErrorInfo/Message`), chosen by the test
  adapter each test names, and refuses a TRX file of an adapter whose layout is
  not known. Before, every skip in a Microsoft Testing Platform result failed
  as a skip without a reason.

## [0.4.0] - 2026-11-25

### Changed

- The publication is the composite action `.github/actions/release-publish`
  instead of the reusable workflow `release-publish.yml`, which is removed.
  nuget.org Trusted Publishing accepts only a token whose `job_workflow_ref`
  names the policy's workflow file in the product repository, and a job of a
  reusable workflow from this repository names this repository there. The
  product repository defines the `publish` job in its release workflow with
  `environment`, permissions, concurrency and timeout, and passes the tag and
  `nuget-user` to the action.
- The publish job reads its own OIDC token and requires the policy's
  repository, `release.environment` and `release.workflowPath` in its
  `repository`, `environment` and `job_workflow_ref` claims.
- The release admission is signed by the product repository's release
  workflow and verified with that workflow as signer.
- `workflow-lint.yml` audits `.github/actions` with zizmor when it exists.

## [0.3.0] - 2026-11-25

### Added

- `api-reference.yml`: after a publication, builds the API reference of the
  shipped packages and the documents under `docs/` with DocFX 2.81.0 from the
  released tag and deploys it to GitHub Pages. A pre-release replaces the site
  only while no stable release exists. The navigation follows a fixed format
  of `docs/README.md` and lists the decision records from the index that
  `madr-check` writes. Links to repository files outside the site point to
  GitHub at the released tag; the checkout keeps its relative links.
- `architecture-document.yml`: restores the solution and holds
  `docs/architecture.md` to the code with `as-built-dotnet` 0.1.0, a container
  image pinned by digest, without network and on read-only mounts.

### Changed

- The release preflight accepts only the Keep a Changelog heading
  `## [X.Y.Z] - YYYY-MM-DD`; the form `## X.Y.Z (YYYY-MM-DD)` now fails.
  The link reference definitions at the end of the file stay out of the
  release notes of the oldest version.
- `decision-records.yml` runs `madr-check` 0.1.1.
- `pre-tag` runs every check, also after a failure, and prints one `OK` or
  `FAIL` line per check with a remedy, the number of failed checks, and the
  tag and push commands when all pass. It exits with 1 when a check failed.

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
