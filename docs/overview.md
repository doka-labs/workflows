# Overview

How the reusable workflows of this repository fit together, what each one
checks, what it may write, and what happens when a step fails. Every name
below is a workflow file, job, step, input, artifact or function of this
repository; the diagrams follow the code, not a plan.

Product repositories do not call these workflows yet. Where a diagram shows
the product side, it shows the intended integration; the caller's own
workflow and job names are the product's choice.

## 1. Building blocks and their callers

A product repository keeps its own workflow files. They call the reusable
workflows of this repository by commit SHA; each reusable workflow checks out
`tools/` from its own commit (`job.workflow_repository` at `job.workflow_sha`)
and runs it against the caller's checkout.

```mermaid
flowchart LR
  subgraph product["Product repository (caller workflows)"]
    ci["pull request workflow<br/>pull_request"]
    schedule["schedule workflow on main<br/>schedule"]
    workload["workload workflow<br/>schedule, workflow_dispatch"]
    rc["release workflow<br/>workflow_dispatch"]
  end

  subgraph helper["doka-labs/workflows"]
    lint["workflow-lint.yml"]
    scope["change-scope.yml"]
    style["code-style.yml"]
    pkg["package-check.yml"]
    req["required-gate.yml"]
    assign["pr-assign.yml"]
    check["release-policy-check.yml"]
    review["dependency-review.yml"]
    cov["coverage-gate.yml"]
    prat["release-pr-attestation.yml"]
    coord["release-lines-coordinator.yml"]
    guard["release-line-guard.yml"]
    audit["dependency-audit.yml"]
    pre["release-preflight.yml"]
    qual["release-qualification.yml"]
    cand["release-candidate.yml"]
    pub["release-publish.yml"]
    tools[("tools/ at job.workflow_sha")]
  end

  ci --> scope & lint & check & review & style & pkg & cov & req & prat & assign
  schedule --> coord
  coord -. "workflow_dispatch per active line" .-> workload
  workload --> guard --> audit
  rc --> pre --> qual --> cand --> pub
  helper -. "every job checks out" .-> tools
```

## 2. What a product repository provides

```mermaid
flowchart LR
  subgraph files["Files in the product repository"]
    rp[".github/release-policy.json"]
    cp[".github/coverage-policy.json"]
    cs[".github/change-scope.json"]
    dt[".github/dependabot-template.yml"]
    dy[".github/dependabot.yml (rendered)"]
    as[".github/allowed_signers"]
    cl["CHANGELOG.md section per version"]
    gj["global.json, Directory.Build.props,<br/>lockfiles"]
  end

  subgraph jobs["Jobs and artifacts of the caller workflows"]
    build["build and pack job"] --> pa[("packages artifact<br/>.nupkg, .snupkg<br/>input packages-artifact")]
    tests["test jobs"] --> trx[("TRX results<br/>input test-results-pattern")]
    tests --> cob[("Cobertura reports<br/>input reports-pattern")]
    gate["gate jobs named in<br/>qualification.gates"]
  end

  subgraph settings["Repository settings"]
    env["environment, default nuget:<br/>required reviewers, exactly the active<br/>lines as branch grants, no wildcards"]
    sec["secret NUGET_USER<br/>(Trusted Publishing user)"]
    rs["every line branch protected"]
  end

  rp --> check2["release-policy-check, coordinator, guard,<br/>preflight, qualification, candidate, publish"]
  cp --> cov2["coverage-gate"]
  cs --> sc2["change-scope, release-pr-attestation,<br/>release-qualification"]
  dt & dy --> check2
  as --> pub2["publish, pre-tag"]
  cl --> pre2["preflight, candidate"]
  gj --> check2
  pa --> cand2["release-candidate"]
  trx --> qual2["release-qualification"]
  cob --> cov2
  gate --> prat2["release-pr-attestation, release-qualification"]
  env & sec --> pub2
  rs --> pub2
  rs --> pre2
```

## 3. The release policy and who reads it

`.github/release-policy.json` is read from the current head of protected
`main` through the API whenever a step grants something (dispatch, admission,
publication). The pull request check takes the allowed base lines from that
copy on `main` and checks the toolchain and `dependabot.yml` against the
checkout's own copy, so a pull request can change the policy.

```mermaid
flowchart TB
  policy[".github/release-policy.json"]
  policy --> lines["lines<br/>sourceRef, role, packageMajor,<br/>channel, upstreamStage, state"]
  policy --> release["release<br/>workflowPath, environment, packages"]
  policy --> toolchain["toolchain<br/>frameworkPackages, majorCeilingPackages,<br/>coherentGroups, lockfiles, versionPrefix, coupling"]
  policy --> workloads["workloads<br/>id, workflow, requiredJobs, timeoutMinutes"]
  policy --> qualification["qualification<br/>gates (protected-check, candidate-produced),<br/>testResults.skips"]

  lines --> u1["PR base allowed?<br/>release-policy-check"]
  lines --> u2["which lines to dispatch<br/>coordinator, guard"]
  lines --> u3["may this line qualify or publish this version?<br/>preflight, qualification, candidate, publish"]
  toolchain --> u4["SDK, target frameworks, lockfiles fit the line<br/>release-policy-check"]
  toolchain --> u5["Dependabot boundaries<br/>render-dependabot"]
  workloads --> u2
  qualification --> u6["gates and accepted skips<br/>pr-attestation, qualification"]
  release --> u7["release workflow entry, publisher environment,<br/>package IDs for unused-version, validation, coverage"]
```

## 4. Release lines

Each line is one branch. Role, ref, channel and upstream stage are tied
together (`_line` in `tools/release_lines/policy.py`); the state decides what
the line may be used for (`PURPOSES`). Changing a line is a pull request
against `main`; the policy prescribes no order of states.

```mermaid
flowchart LR
  subgraph roles["Role and its fixed form"]
    main["main<br/>refs/heads/main<br/>GA upstream, stable-and-prerelease"]
    maint["maintenance<br/>refs/heads/maintenance/N.x<br/>GA upstream, stable-and-prerelease"]
    prev["preview<br/>refs/heads/preview/N.x<br/>ga, preview or rc upstream, prerelease only"]
  end

  subgraph states["State and what it allows"]
    inactive["inactive"] --> p1["PR base"]
    prepared["prepared"] --> p1
    prepared --> p2["qualification only<br/>(qualificationOnly snapshot)"]
    active["active"] --> p1
    active --> p3["qualification"]
    active --> p4["publication"]
    active --> p5["maintenance dispatch"]
  end
```

Exactly one main line; every `sourceRef` once; at most one active line per
package major, so two branches never publish into the same versions; a
version must have the major of its line.

## 5. Pull request

```mermaid
flowchart LR
  pr(["pull_request"]) --> scope["change-scope<br/>git diff from base to merge commit;<br/>docs only for declared paths that no build input<br/>and no tracked non-documentation file names"]
  pr --> lint["workflow-lint"]
  pr --> assign["pr-assign<br/>author as assignee,<br/>not for bots or forks"]
  pr --> check["release-policy-check<br/>PR base is a line of the policy on main,<br/>checkout's policy valid, toolchain fits the base line,<br/>dependabot.yml equals rendered"]
  pr --> review["dependency-review"]
  scope -- full --> heavy["code-style, product build and tests,<br/>package-check, coverage-gate"]
  scope -- docs --> docs["the repository's documentation checks"]
  heavy --> req["required-gate<br/>every needed job succeeded, or skipped<br/>for docs and listed in skippable"]
  docs --> req
  scope --> req
  req --> prat["release-pr-attestation<br/>checkout is the tested merge commit,<br/>gate jobs succeeded, scope and base tree recomputed"]
  prat --> att[("pr-qualification-v1<br/>subject: git-tree-&lt;tree&gt;.txt,<br/>scope docs or full")]
```

The attestation is what a later release of a commit with the same tree relies
on; it does not expire like an artifact. A required check whose workflow a path
filter skips stays pending, so the CI workflow always starts and only its jobs
are skipped.

## 6. Checks of every change in detail

```mermaid
flowchart TB
  subgraph lintbox["workflow-lint"]
    l1["actionlint 1.7.12, checksum-verified"] --> l2["zizmor 1.32.0 from hash-pinned<br/>requirements, offline, persona regular"] --> l3["shellcheck on every tracked *.sh,<br/>severity warning"]
  end

  subgraph reviewbox["dependency-review"]
    r1["dependency-review-action<br/>fail-on-severity, allow-licenses,<br/>license exceptions as allowed packages"] --> r2{"license exceptions set?"}
    r2 -- yes --> r3["read the dependency comparison base...head<br/>from the API, every page"] --> r4["an added package that matches an exception<br/>must be NuGet at exactly the reviewed version"]
    r2 -- no --> r5(["done"])
  end

  subgraph auditbox["dependency-audit (scheduled workload)"]
    a1["solution of every project file,<br/>NuGet.Config with nuget.org only"] --> a2["restore --force --no-http-cache,<br/>locked mode, audit all, level low,<br/>NU1900-NU1905 as errors"] --> a3["dotnet package list --vulnerable<br/>--include-transitive, JSON v1"] --> a4["no diagnostics, every project listed,<br/>no vulnerable package"] --> a5[("evidence artifact<br/>dependency-audit-&lt;run&gt;-&lt;attempt&gt;")]
  end

  subgraph covbox["coverage-gate"]
    c1["merge Cobertura reports line by line,<br/>source paths from /src/ on"] --> c2["no empty report,<br/>optional maximum age"] --> c3["every release package has a threshold<br/>and executed lines"] --> c4["line and branch floors per assembly<br/>and per critical class; branch floor null<br/>only for a branch-free surface"]
  end
```

## 7. Release-line guard

The first job of a workload decides whether the rest may run.

```mermaid
flowchart TB
  start(["workload run"]) --> m{"maintenance input<br/>or schedule event?"}
  m -- "neither: pull request, push,<br/>manual run" --> go1(["proceed"])
  m -- maintenance --> d1{"workflow_dispatch, GITHUB_SHA equals<br/>schedule-source-sha, valid correlation,<br/>running workflow is a declared workload?"}
  d1 -- no --> stop1(["fail: authority_mismatch"])
  d1 -- yes --> d2{"active line besides main?"}
  d2 -- no --> stop1
  d2 -- yes --> go2(["proceed"])
  m -- schedule --> s1{"on the current head of main,<br/>declared workload?"}
  s1 -- no --> stop1
  s1 -- yes --> s2{"main is an active line?"}
  s2 -- yes --> go3(["proceed"])
  s2 -- no --> skip(["skip: proceed=false"])
```

## 8. Scheduled maintenance of all active lines

```mermaid
sequenceDiagram
  autonumber
  participant S as schedule workflow on main
  participant C as release-lines-coordinator
  participant API as GitHub API
  participant W as workload workflow on a line
  participant G as release-line-guard

  S->>C: workload id
  C->>API: head of protected main, release policy at that commit (Git blob checked)
  C->>C: require run on current head of main, workload declared
  loop every active line besides main, child runs parallel, REST calls serial and capped at 480 per rolling hour
    C->>API: head of the protected line
    C->>API: workflow_dispatch(maintenance, schedule-correlation, schedule-source-sha)
    API-->>C: workflow_run_id
    API->>W: run on the line
    W->>G: maintenance inputs
    G->>API: policy on main
    G-->>W: proceed only for an active line at the resolved commit
    loop until completed or workload budget
      C->>API: run state (404 and default title tolerated for 300 s,<br/>missing conclusion tolerated for 120 s)
    end
    C->>API: jobs of the latest attempt
    C->>C: required jobs succeeded, no job failed, cancelled or timed out
  end
  C->>C: report artifact per line, one failing line never hides another
```

## 9. Release run

One run per version, started by `workflow_dispatch` on the line's branch. A
later run of the same version finds the draft and continues at publication.

```mermaid
flowchart TB
  start(["workflow_dispatch<br/>version, qualification-only"]) --> pre["release-preflight<br/>see 10"]
  pre --> resume{"release with candidate.json<br/>for this version, a draft complete?"}
  resume -- "yes: resume" --> publish
  resume -- no --> build["product jobs<br/>build, tests (TRX), packages"]
  build --> qual["release-qualification<br/>see 11"]
  qual --> only{"qualification-only?"}
  only -- yes --> done1(["end: qualified, never published"])
  only -- no --> cand["release-candidate / Candidate<br/>see 12"]
  cand --> sbom["release-candidate / SBOM<br/>one SBOM attestation per package"]
  cand --> tag["maintainer: pre-tag check (immutable releases<br/>enabled, draft passes the publish verification),<br/>SSH-signed annotated tag v&lt;version&gt;, push"]
  tag --> approve["environment approval"]
  sbom --> publish
  approve --> publish["release-publish<br/>see 13"]
  publish --> done2(["immutable release, packages on nuget.org"])
```

## 10. Preflight

```mermaid
flowchart TB
  p1["started by workflow_dispatch from release.workflowPath"] --> p2["line of the ref may publish<br/>(or qualify for qualification-only)"]
  p2 --> p3["run is on the current head of the protected line"] --> p4["CHANGELOG.md has a reviewed section"]
  p4 --> r{"release of v&lt;version&gt; exists?<br/>(drafts need contents: write)"}
  r -- yes --> r1{"qualification-only?"}
  r1 -- yes --> f1(["fail: qualify another version"])
  r1 -- no --> r2{"has candidate.json?"}
  r2 -- no --> f2(["fail: delete the draft to start over"])
  r2 -- yes --> r3{"draft: every expected asset uploaded unchanged,<br/>and the publish job's candidate verification passes<br/>(provenance, exact SBOM content, qualification)?"}
  r3 -- no --> f3(["fail: re-run the failed candidate jobs,<br/>or delete the draft and start a new run"])
  r3 -- yes --> out1(["resume=true"])
  r -- no --> t1["tag v&lt;version&gt; does not exist"] --> t2["commit carries no other tag"] --> t3["version absent on nuget.org<br/>for every release package"] --> out2(["resume=false"])
```

## 11. Qualification

```mermaid
flowchart TB
  q1["checkout tree equals the run's commit tree"] --> q2["freeze the line from the policy on main<br/>(snapshot, qualificationOnly for prepared lines)"]
  q2 --> q3{"protected-check gates?"}
  q3 -- yes --> q4["verify pr-qualification-v1 for this tree, line and workflow;<br/>scope docs: walk back through baseTree to a full one,<br/>recompute every step with git diff (at most 50);<br/>every protected gate succeeded in the full one"]
  q3 -- no --> q5
  q4 --> q5{"candidate-produced gates?"}
  q5 -- yes --> q6["those jobs of this run succeeded"]
  q5 -- no --> q7
  q6 --> q7["TRX of every test project: no failure,<br/>no duplicate, skips only where the policy<br/>accepts them and with a reason"]
  q7 --> q8["every release package has one packable project<br/>with EnablePackageValidation=true"]
  q8 --> q9[("attest release-line-snapshot-v1<br/>and release-qualification-v1, subject: tree")]
```

## 12. Candidate job

```mermaid
flowchart TB
  c1["download the packages artifact"] --> c2["install sbom-tool 4.1.5, checksum-verified"]
  c2 --> c3["per package: isolated consumer restores it from<br/>a feed of the candidate packages only,<br/>restored archives byte-identical to the candidate,<br/>no test or build tooling in the graph"]
  c3 --> c4["per package: SPDX 2.2 SBOM from that consumer,<br/>validated by sbom-tool, exact dependency closure,<br/>describes exactly the candidate archive"]
  c4 --> c5["Portable PDBs match their assemblies,<br/>symbol probes derived (tools/NuGetSymbols)"]
  c5 --> c6["packages bound to repository and commit,<br/>exactly release.packages, versions unused,<br/>write candidate.json"]
  c6 --> c7[("attest SLSA provenance<br/>packages and candidate.json")]
  c7 --> c8[("artifact release-candidate-sboms-&lt;version&gt;<br/>sboms.json: every SBOM with its digest<br/>and its package's digest")]
  c8 --> c9["stage: draft created if missing, title, notes and<br/>prerelease flag equal; one stage per version at a time;<br/>with candidate.json in the draft every asset must be<br/>equal (hashed when GitHub reports no digest), without it<br/>differing assets of an unfinished attempt are replaced;<br/>candidate.json last"]
  c8 --> s1["SBOM job per package: packages artifact and sboms.json,<br/>write the SBOM, both digests equal the candidate"] --> s2[("attest SPDX SBOM, subject: .nupkg")]
```

## 13. Publish job

Every irreversible write is preceded by a full recheck. A stop anywhere is
resumed by re-running the job: what nuget.org and the draft already hold is
observed, never replaced.

```mermaid
flowchart TB
  dl["download every draft asset by release ID"] --> auth["authorize<br/>assets equal candidate.json, provenance covers every package,<br/>SBOM attestations equal the SBOMs, qualification from the candidate's run,<br/>policy on main allows publication, environment grants exactly the active lines,<br/>signed tag verified locally and by GitHub, commit on the line,<br/>earlier admissions name the same tag object"]
  auth --> adm[("attest release-admission-v1")]
  adm --> pending["list packages nuget.org does not hold<br/>(other bytes there: stop)"]
  pending --> login["NuGet/login: short-lived key<br/>through Trusted Publishing"]
  login --> recheck["recheck authorization, Latest and live draft assets"]
  recheck --> push["dotnet nuget push in dependency order:<br/>pending primaries with --no-symbols,<br/>then every symbol package, --skip-duplicate"]
  push --> readback{"signed packages and Portable PDBs<br/>served within one hour?"}
  readback -- "no: end without failure" --> rerun(["re-run the job later"])
  readback -- yes --> consumer["restore every package from nuget.org only;<br/>payload equals the candidate"]
  consumer --> recheck2["recheck authorization"]
  recheck2 --> cur["Latest decided, then the current draft<br/>assets equal the candidate"]
  cur --> rel["publish the draft by numeric ID,<br/>make_latest only for a strictly newer stable version"]
  rel --> verify["release immutable, gh release verify,<br/>gh release verify-asset for every asset,<br/>whole asset inventory equals the candidate,<br/>Latest confirmed"]
```

## 14. Data between jobs

```mermaid
flowchart LR
  build["product build"] --> pa[("packages artifact")] --> cand["Candidate"]
  tests["product tests"] --> trx[("TRX artifacts")] --> qual["Qualification"]
  tests --> cob[("Cobertura artifacts")] --> cov["coverage-gate"]
  cand --> sa[("release-candidate-sboms-&lt;version&gt;<br/>sboms.json")] --> sbom["SBOM jobs"]
  pa --> sbom
  cand --> draft[("draft release<br/>candidate.json, .nupkg, .snupkg, .spdx.json,<br/>release-notes.md, symbol-probes.json")]
  draft --> pub["Publish"]
  draft --> pretag["pre-tag (local)"]
  prat["PR attestation"] --> store[("GitHub attestations")]
  qual --> store
  cand --> store
  sbom --> store
  pub --> store
  store --> qual
  store --> pub
  pub --> nuget[("nuget.org")]
  pub --> release[("immutable release")]
  audit["dependency-audit"] --> ev[("audit evidence artifact")]
  coord["coordinator"] --> rep[("coordination report artifact")]
```

Artifacts live only as long as the retention setting; everything a later run
relies on is an attestation or a release asset.

## 15. Permissions

Each job asks for the least the API calls need; the top-level default of every
workflow is `contents: read`.

```mermaid
flowchart TB
  subgraph read["contents: read only"]
    r1["workflow-lint"]
    r2["release-policy-check"]
    r3["dependency-review"]
    r4["dependency-audit"]
    r5["release-line-guard"]
    r6["coverage-gate (+ actions: read)"]
  end
  subgraph dispatch["dispatches runs"]
    d1["release-lines-coordinator<br/>actions: write"]
  end
  subgraph attest["signs attestations: id-token, attestations write"]
    a1["release-pr-attestation (+ actions: read)"]
    a2["release-qualification (+ actions: read)"]
    a3["release-candidate / SBOM (+ actions: read)"]
  end
  subgraph releases["writes or sees draft releases: contents write"]
    w1["release-preflight<br/>(only to see drafts) + attestations: read"]
    w2["release-candidate / Candidate<br/>+ id-token, attestations, actions: read"]
    w3["release-publish<br/>+ actions: read, id-token, attestations,<br/>environment approval, secret NUGET_USER"]
  end
  w3 --> nuget["nuget.org Trusted Publishing:<br/>OIDC token -> short-lived API key"]
```

## 16. Failure and resume

```mermaid
flowchart TB
  f1["preflight fails"] --> x1["nothing written; fix line, notes or version,<br/>start a new run"]
  f2["product job or qualification fails"] --> x2["nothing published; re-run the failed jobs<br/>or start a new run"]
  f3["Candidate fails"] --> x3["re-run: stage keeps matching assets, replaces<br/>those of the unfinished attempt and uploads<br/>candidate.json last; a complete candidate in<br/>the draft is never replaced"]
  f4["SBOM job fails"] --> x4["re-run the failed SBOM jobs; whether they find<br/>the Candidate's artifact of the earlier attempt<br/>is not documented. Otherwise delete the draft<br/>and start a new run"]
  f5["run cancelled or gone after the draft exists"] --> x5["new run of the same version: preflight resumes<br/>at publication only for a complete draft,<br/>otherwise it stops and names what is missing"]
  f6["publish stops before or during the push"] --> x6["re-run: primaries already on nuget.org with the<br/>candidate bytes are skipped, every symbol<br/>package is pushed again (409 is a warning)"]
  f7["nuget.org not serving within one hour"] --> x7["job ends without failure, draft stays;<br/>re-run later"]
  f8["one maintenance line fails"] --> x8["coordinator reports it; the other lines<br/>are still dispatched and checked"]
```

## 17. Attestations and what checks them

All attestations are signed by a reusable workflow of this repository, so
`gh attestation verify --signer-workflow` accepts nothing else.

```mermaid
flowchart LR
  prat["release-pr-attestation"] -- signs --> a1[("pr-qualification-v1<br/>subject: tree")]
  qual["release-qualification"] -- signs --> a2[("release-line-snapshot-v1<br/>subject: tree")]
  qual -- signs --> a3[("release-qualification-v1<br/>subject: tree")]
  cand["release-candidate"] -- signs --> a4[("SLSA provenance<br/>subjects: packages, candidate.json")]
  cand -- signs --> a5[("SPDX SBOM per package<br/>subject: .nupkg")]
  pub["release-publish"] -- signs --> a6[("release-admission-v1<br/>subject: candidate.json")]
  gh["GitHub"] -- "on publication" --> a7[("release attestation<br/>immutable release")]

  a1 -- "verified by" --> qual
  a3 -- "verified by" --> pub
  a4 -- "verified by" --> pub
  a5 -- "verified by" --> pub
  a6 -- "earlier ones verified by" --> pub
  a7 -- "verified by" --> pub
```

## 18. Dependabot configuration

```mermaid
flowchart LR
  t[".github/dependabot-template.yml<br/>one entry per ecosystem and directory,<br/>no target-branch"] --> r["render-dependabot"]
  p["release-policy.json<br/>active lines, toolchain"] --> r
  r --> o["dependabot.yml<br/>every entry once per active line,<br/>SDK without major updates,<br/>framework and ceiling packages below the next major"]
  o --> c{"release-policy-check:<br/>committed file equals rendered?"}
  c -- no --> f(["check fails, expected file in the job summary"])
  c -- yes --> ok(["check passes"])
```

## 19. This repository itself

```mermaid
flowchart LR
  change["change in doka-labs/workflows"] --> sc["self-check.yml on pull_request<br/>and push to main"]
  sc --> lint["Lint: workflow-lint.yml via $/"]
  sc --> tests["Tool Tests: unit tests of tools/<br/>with the SDK of global.json"]
  lint & tests --> merge["merge to main<br/>with a dated CHANGELOG.md section"]
  merge --> tag["administrator: SSH-signed annotated tag<br/>v&lt;version&gt; on the merge commit, push"]
  tag --> rel["release.yml on the tag push"]
  rel --> r1{"tag verified by GitHub, commit on main,<br/>Self Check of the commit passed?<br/>(waits up to 20 minutes)"}
  r1 -- no --> rf(["fail, no release"])
  r1 -- yes --> r2["release with the notes of the<br/>CHANGELOG.md section, Latest when newest"]
  r2 --> r3["immutable and attested,<br/>checked with gh release verify"]
  r3 --> dep["Dependabot in each product repository<br/>proposes the new SHA and version comment"]
  dep --> prr["product pull request runs the<br/>reusable workflows at the new SHA"]
```
