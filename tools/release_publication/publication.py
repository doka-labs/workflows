"""Release flow: preflight, candidate in a draft release, signed tag, NuGet publication and immutable release.

One workflow run per version. A later run of the same version finds the
valid draft in the preflight and continues with publication, so long test runs
do not repeat. The checks are the union of those of MySql, SafeMigrations and
NestedSet; each names its origin.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import time
from datetime import date
from pathlib import Path
from urllib.parse import quote

from tools.release_lines import attestation
from tools.release_lines import policy as policies
from tools.release_lines.coordinator import observe_authority, protected_head
from tools.release_lines.github import GitHubError
from tools.release_publication import nuget, packages

CANDIDATE = "candidate.json"
NOTES = "release-notes.md"
PROBES = "symbol-probes.json"
ALLOWED_SIGNERS = ".github/allowed_signers"
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
# WHY: Two heading forms are in use: Keep a Changelog "## [X.Y.Z] - YYYY-MM-DD" (MySql, SafeMigrations) and
# "## X.Y.Z (YYYY-MM-DD)" (NestedSet). https://keepachangelog.com/en/1.1.0/
HEADING = re.compile(r"^## (?:\[(?P<a>[^\]]+)\] - (?P<a_date>[0-9]{4}-[0-9]{2}-[0-9]{2})"
                     r"|(?P<b>\S+) \((?P<b_date>[0-9]{4}-[0-9]{2}-[0-9]{2})\))[ \t]*$", re.MULTILINE)
WILDCARD = re.compile(r"[*?\[]")


class PublicationError(RuntimeError):
    """A release step cannot prove that it acts on the qualified candidate under current authority."""


class ReleaseInventoryError(PublicationError):
    """The published release holds other assets than the candidate; retrying cannot change that."""


def require(condition: bool, message: str) -> None:
    """Raise a publication error when a condition does not hold."""
    if not condition:
        raise PublicationError(message)


def run(command: list[str], *, cwd: Path | None = None, timeout: int = 300) -> str:
    """Run a command and return its output; a failure names the command, never its output."""
    completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    require(completed.returncode == 0, f"Command failed: {' '.join(command[:3])}")
    return completed.stdout


def read_json(path: Path) -> dict:
    """Read a bounded regular JSON object and reject duplicate keys (all three repositories)."""
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_MANIFEST_BYTES,
            f"{path.name} must be a regular file of at most 16 MiB.")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f"{path.name} repeats the key {key}.")
            result[key] = value

        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    except ValueError as error:
        raise PublicationError(f"{path.name} is not valid JSON.") from error

    require(isinstance(value, dict), f"{path.name} must hold a JSON object.")
    return value


def release_notes(root: Path, version: str) -> str:
    """Return the notes of the version's single dated changelog section."""
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    headings = [match for match in HEADING.finditer(text) if (match["a"] or match["b"]) == version]
    require(len(headings) == 1, f"CHANGELOG.md needs exactly one dated section for {version}.")
    match = headings[0]
    try:
        date.fromisoformat(match["a_date"] or match["b_date"])
    except ValueError as error:
        raise PublicationError(f"CHANGELOG.md has an invalid date for {version}.") from error

    following = re.search(r"^## ", text[match.end():], re.MULTILINE)
    body = text[match.end():match.end() + following.start() if following else len(text)].strip()
    require(bool(body), f"CHANGELOG.md has no notes for {version}.")
    return f"# {version}\n\n{body}\n"


def require_release_workflow(policy: dict, environment: dict) -> None:
    """Require the run to come from the release workflow the policy names (MySql, SafeMigrations)."""
    expected = f"{policy['repository']}/{policy['release']['workflowPath']}@{environment.get('GITHUB_REF')}"
    require(environment.get("GITHUB_WORKFLOW_REF") == expected,
            f"Releases run only from {policy['release']['workflowPath']}.")


def release_for(api, repository: str, tag: str) -> dict | None:
    """Return the single release, draft or published, that carries the tag name."""
    # WHY: "Only users with push access will receive listings for draft releases", so the job token needs
    # contents: write to see a draft. https://docs.github.com/en/rest/releases/releases#list-releases
    found = []
    for page in range(1, 101):
        entries = api.request(f"repos/{repository}/releases?per_page=100&page={page}")
        require(isinstance(entries, list), "The release listing is invalid.")
        found.extend(entry for entry in entries if entry.get("tag_name") == tag)
        if len(entries) < 100:
            break

    require(len(found) <= 1, f"More than one release claims {tag}.")
    return found[0] if found else None


def release_assets(api, repository: str, release: dict) -> dict[str, dict]:
    """Return the assets of a release by name."""
    assets = {}
    for page in range(1, 11):
        entries = api.request(f"repos/{repository}/releases/{release['id']}/assets?per_page=100&page={page}")
        require(isinstance(entries, list), "The release asset listing is invalid.")
        for entry in entries:
            require(entry.get("name") not in assets, f"The release has the asset {entry.get('name')} twice.")
            # WHY: Downloads address numeric IDs, so two names must never share one remote resource (MySql
            # verify_release_assets).
            require(type(entry.get("id")) is int and entry["id"] not in {item["id"] for item in assets.values()},
                    f"The release asset {entry.get('name')} has no unique ID.")
            assets[entry["name"]] = entry

        if len(entries) < 100:
            return assets

    raise PublicationError("The release asset listing exceeded its page bound.")


def asset_bytes(repository: str, asset: dict) -> bytes:
    """Download one release asset by ID; drafts included, the size must match."""
    # WHY: GET /releases/tags/{tag} does not return drafts, and a draft has no tag yet; the authenticated release
    # list and the asset endpoint with Accept: application/octet-stream do.
    # https://docs.github.com/en/rest/releases/assets
    data = subprocess.run(["gh", "api", "-H", "Accept: application/octet-stream",
                           f"repos/{repository}/releases/assets/{asset['id']}"], capture_output=True, timeout=600,
                          check=False)
    require(data.returncode == 0 and len(data.stdout) == asset.get("size"), f"Cannot download {asset.get('name')}.")
    return data.stdout


def draft_asset_problems(repository: str, assets: dict[str, dict], expected: dict[str, str]) -> list[str]:
    """Name every asset that is missing, unexpected, unfinished or other bytes than the expected digest."""
    problems = sorted(set(expected) ^ set(assets))
    for name, digest in expected.items():
        asset = assets.get(name)
        if asset is None:
            continue

        # WHY: GitHub reports each asset's digest as "sha256:<hex>"; without one the bytes are hashed here.
        # https://docs.github.com/en/rest/releases/assets
        actual = asset.get("digest") or f"sha256:{hashlib.sha256(asset_bytes(repository, asset)).hexdigest()}"
        if asset.get("state") != "uploaded" or actual != f"sha256:{digest}":
            problems.append(name)

    return problems


def require_complete_draft(api, repository: str, release: dict, *, verify=attestation.verify) -> None:
    """Require a draft that a resumed run can publish: the publish job's own candidate verification passes."""
    assets = release_assets(api, repository, release)
    data = asset_bytes(repository, assets[CANDIDATE])
    manifest = json.loads(data)
    expected = {**expected_assets(manifest), CANDIDATE: hashlib.sha256(data).hexdigest()}
    problems = draft_asset_problems(repository, assets, expected)
    # WHY: A resumed run skips the candidate jobs, so a draft they did not finish could never be published; a new
    # build would produce other bytes than the staged ones, which stage refuses.
    hint = "re-run the failed candidate jobs of its run, or delete the draft and start a new run"
    require(not problems, f"The draft of {release.get('tag_name')} is incomplete ({', '.join(problems)}); {hint}.")
    # WHY: An SBOM attestation of the same package digest from an earlier attempt can carry another SBOM; only the
    # check publish runs (exact SBOM content, provenance, qualification) proves the draft publishable.
    with tempfile.TemporaryDirectory(prefix="preflight-draft-") as directory:
        for name, asset in assets.items():
            (Path(directory) / name).write_bytes(data if name == CANDIDATE else asset_bytes(repository, asset))

        try:
            verify_candidate(repository, Path(directory), verify=verify)
        except (PublicationError, GitHubError, nuget.NuGetError) as error:
            raise PublicationError(f"The draft of {release.get('tag_name')} cannot be published ({error}); "
                                   f"{hint}.") from error


def tag_exists(api, repository: str, tag: str) -> bool:
    """Return whether the tag exists on GitHub."""
    try:
        api.request(f"repos/{repository}/git/ref/tags/{quote(tag, safe='')}")
    except GitHubError as error:
        if error.code == "github_http_error" and error.http_status == 404:
            return False

        raise

    return True


def tags_on_commit(root: Path, commit: str) -> list[str]:
    """Return the remote tags that point to a commit, annotated or lightweight."""
    tags = set()
    for line in run(["git", "-C", str(root), "ls-remote", "--tags", "origin"]).splitlines():
        sha, _, ref = line.partition("\t")
        if sha == commit and ref.startswith("refs/tags/"):
            tags.add(ref.removeprefix("refs/tags/").removesuffix("^{}"))

    return sorted(tags)


def preflight(api, environment: dict, root: Path, *, version: str, qualification_only: bool,
              fetcher=nuget.fetch) -> dict:
    """Check intent, line, version, notes and the tag; report whether a valid draft lets the run resume."""
    repository, source_ref = environment["GITHUB_REPOSITORY"], environment["GITHUB_REF"]
    require(environment.get("GITHUB_EVENT_NAME") == "workflow_dispatch", "Releases start by workflow_dispatch.")
    authority = observe_authority(api, repository)
    policy = authority["policy"]
    require_release_workflow(policy, environment)
    policies.select_line(policy, source_ref, version, purpose="qualification" if qualification_only else "publication")
    commit = environment.get("GITHUB_SHA")
    require(commit == protected_head(api, repository, source_ref),
            f"The run is not on the current head of {source_ref}.")
    release_notes(root, version)
    tag = f"v{version}"
    release = release_for(api, repository, tag)
    if release is not None:
        require(not qualification_only, f"{tag} already has a release; qualify another version.")
        require(CANDIDATE in release_assets(api, repository, release),
                f"The release of {tag} has no {CANDIDATE}; delete the draft to start over.")
        if release.get("draft") is True:
            require_complete_draft(api, repository, release)

        return {"resume": True, "tag": tag}

    require(not tag_exists(api, repository, tag), f"{tag} exists without a release; it cannot become a candidate.")
    # WHY: A commit that already carries another release tag was released or prepared before (MySql, SafeMigrations
    # and NestedSet).
    others = tags_on_commit(root, commit)
    require(not others, f"{commit} already carries the tags {', '.join(others)}.")
    # WHY: Proving the version unused before the long test runs avoids wasting them (NestedSet's preflight).
    states = nuget.unused(policy["release"]["packages"], version, fetcher=fetcher)
    taken = sorted(name for name, state in states.items() if state != "absent")
    require(not taken, f"{version} is already on nuget.org for {', '.join(taken)}.")
    return {"resume": False, "tag": tag}


def check_packages(policy: dict, root: Path, directory: Path, version: str, repository: str, commit: str,
                   probes: Path) -> tuple[list[dict], list[dict]]:
    """Check identity, source binding, frameworks and Portable PDB probes of the built packages.

    The pull request package check and the candidate run the same checks, so a package problem shows up in the
    pull request instead of first at a release.
    """
    from tools.release_lines.toolchain import target_frameworks

    inventory = packages.inventory(directory, version, repository, commit,
                                   target_frameworks(root, policy["toolchain"]["coupling"]))
    require({item["id"] for item in inventory} == set(policy["release"]["packages"]),
            "The built packages differ from release.packages of the release policy.")
    symbols = read_json(probes)["probes"]
    nuget.validate_symbol_probes(symbols)
    require({probe["package"] for probe in symbols} == {item["nupkg"] for item in inventory},
            "Every package needs its Portable PDB probes.")
    return inventory, symbols


def candidate(api, environment: dict, root: Path, *, version: str, directory: Path, probes: Path,
              output: Path) -> dict:
    """Check the built packages and SBOMs of the qualified commit and write the candidate assets."""
    repository, source_ref, commit = (environment["GITHUB_REPOSITORY"], environment["GITHUB_REF"],
                                      environment["GITHUB_SHA"])
    policy = observe_authority(api, repository)["policy"]
    require_release_workflow(policy, environment)
    policies.select_line(policy, source_ref, version, purpose="qualification")
    inventory, symbols = check_packages(policy, root, directory, version, repository, commit, probes)
    states = nuget.availability(directory, inventory)
    require(all(state["state"] == "absent" for state in states),
            "A package version is already on nuget.org; a new candidate needs an unused version.")
    sboms = {}
    for item in inventory:
        path = directory / f"{item['id']}.spdx.json"
        spdx_version = read_json(path).get("spdxVersion", "")
        require(re.fullmatch(r"SPDX-[0-9]+\.[0-9]+", spdx_version) is not None, f"{path.name} is not an SPDX document.")
        # WHY: actions/attest names SPDX attestations https://spdx.dev/Document/v<version of spdxVersion> and
        # uses the SBOM itself as the predicate (src/sbom.ts of v4.2.2).
        sboms[path.name] = {"sha256": packages.sha256(path), "package": item["nupkg"],
                            "predicateType": f"https://spdx.dev/Document/v{spdx_version.split('-', 1)[1]}"}

    output.mkdir(parents=True, exist_ok=True)
    (output / NOTES).write_text(release_notes(root, version), encoding="utf-8")
    (output / PROBES).write_bytes(probes.read_bytes())
    manifest = {"schemaVersion": 1, "repository": repository, "version": version, "tag": f"v{version}",
                "sourceRef": source_ref, "commit": commit,
                "tree": run(["git", "-C", str(root), "rev-parse", f"{commit}^{{tree}}"]).strip(),
                "packages": inventory, "symbols": symbols, "sboms": sboms,
                "notesSha256": packages.sha256(output / NOTES), "probesSha256": packages.sha256(output / PROBES)}
    (output / CANDIDATE).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="ascii")
    for path in [*directory.glob("*.nupkg"), *directory.glob("*.snupkg"), *(directory / name for name in sboms)]:
        (output / path.name).write_bytes(path.read_bytes())

    return manifest


def write_sbom_handoff(manifest: dict, directory: Path, path: Path) -> None:
    """Write the candidate's SBOMs and the digests of their packages into one JSON file for the SBOM jobs."""
    # WHY: The SBOM jobs run on other runners. Artifact uploads of this repository are single named text files
    # (tools/repository_checks/artifact_uploads.py), so the SBOMs travel as one JSON file and the packages, which
    # are the subjects, come from the caller's packages artifact the candidate was built from.
    handoff = {"schemaVersion": 1, "packages": {}}
    for name, sbom in manifest["sboms"].items():
        package = next(item for item in manifest["packages"] if item["nupkg"] == sbom["package"])
        handoff["packages"][package["id"]] = {"nupkg": package["nupkg"], "nupkgSha256": package["nupkgSha256"],
                                              "sbomName": name, "sbomSha256": sbom["sha256"],
                                              "sbom": (directory / name).read_text(encoding="utf-8")}

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(handoff, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def extract_sbom(handoff_path: Path, package_id: str, directory: Path) -> Path:
    """Write one package's SBOM next to its package; both must equal the candidate's digests."""
    handoff = read_json(handoff_path)
    require(handoff.get("schemaVersion") == 1 and package_id in handoff.get("packages", {}),
            f"The SBOM handoff has no entry for {package_id}.")
    entry = handoff["packages"][package_id]
    require(packages.sha256(directory / entry["nupkg"]) == entry["nupkgSha256"],
            f"{entry['nupkg']} differs from the candidate.")
    path = directory / entry["sbomName"]
    path.write_text(entry["sbom"], encoding="utf-8")
    require(packages.sha256(path) == entry["sbomSha256"], f"{path.name} differs from the candidate.")
    return path


def expected_assets(manifest: dict) -> dict[str, str]:
    """Return every asset name of the candidate besides the manifest, with its SHA-256.

    The asset set of MySql, SafeMigrations and NestedSet.
    """
    result = {NOTES: manifest["notesSha256"], PROBES: manifest["probesSha256"]}
    for package in manifest["packages"]:
        result[package["nupkg"]] = package["nupkgSha256"]
        result[package["snupkg"]] = package["snupkgSha256"]

    result.update({name: sbom["sha256"] for name, sbom in manifest["sboms"].items()})
    return result


def require_release_metadata(release: dict, manifest: dict, notes: str) -> None:
    """Require title, notes and prerelease flag of the release to match the candidate.

    The rule of MySql, SafeMigrations and NestedSet.
    """
    prerelease = policies.parse_version(manifest["version"]).is_prerelease
    require(release.get("name") == manifest["tag"] and release.get("body") == notes
            and release.get("prerelease") is prerelease,
            f"The release of {manifest['tag']} has another title, notes or prerelease flag than the candidate.")


def stage(api, repository: str, directory: Path) -> dict:
    """Create the draft if missing and upload only missing assets; never replace one of a complete candidate.

    NestedSet's stage.
    """
    manifest = read_json(directory / CANDIDATE)
    expected = {**expected_assets(manifest), CANDIDATE: packages.sha256(directory / CANDIDATE)}
    tag = manifest["tag"]
    release = release_for(api, repository, tag)
    if release is None:
        command = ["gh", "release", "create", tag, "--repo", repository, "--draft", "--target", manifest["commit"],
                   "--title", tag, "--notes-file", str(directory / NOTES)]
        if policies.parse_version(manifest["version"]).is_prerelease:
            command.append("--prerelease")

        run(command)
        release = release_for(api, repository, tag)
        require(release is not None, f"The new draft of {tag} is not visible yet; re-run the job.")

    require(release.get("draft") is True, f"{tag} is already published; it cannot be staged again.")
    require_release_metadata(release, manifest, (directory / NOTES).read_text(encoding="utf-8"))
    present = release_assets(api, repository, release)
    unexpected = sorted(set(present) - set(expected))
    require(not unexpected, f"The draft of {tag} has unexpected assets: {', '.join(unexpected)}.")
    uploaded = {name: asset for name, asset in present.items() if asset.get("state") == "uploaded"}
    # WHY: candidate.json is uploaded last, so its presence marks a complete candidate whose assets are never
    # replaced (NestedSet's validate_release). Without it the draft holds an unfinished earlier attempt; that attempt
    # generated its own SBOMs and manifest, because sbom-tool writes a namespace ".../<packageName>/<packageVersion>/
    # <new-guid>" into every SBOM, so its differing assets are deleted and uploaded again.
    # https://github.com/microsoft/sbom-tool/blob/v4.1.5/README.md
    committed = CANDIDATE in uploaded
    stale = set()
    for name, asset in uploaded.items():
        # WHY: GitHub reports each asset's digest as "sha256:<hex>" (NestedSet v10.4.0: candidate.json digest
        # sha256:23dd49fe...); the documentation only names the field ("digest: string or null"). Without a digest
        # the size cannot tell two SBOMs apart, so the bytes are downloaded and hashed, as before publishing.
        # https://docs.github.com/en/rest/releases/assets
        same = not draft_asset_problems(repository, {name: asset}, {name: expected[name]})
        if not same:
            require(not committed, f"The asset {name} of the draft differs from the candidate.")
            stale.add(name)

    # WHY: "When an upstream failure occurs, you will receive a 502 Bad Gateway status. This may leave an empty asset
    # with a state of starter. It can be safely deleted." Only such unfinished assets are deleted, and only after
    # every uploaded asset matched (MySql reconcile_draft_assets). https://docs.github.com/en/rest/releases/assets
    for name, asset in present.items():
        if name not in uploaded or name in stale:
            api.request(f"repos/{repository}/releases/assets/{asset['id']}", "DELETE")

    missing = sorted(set(expected) - (set(uploaded) - stale), key=lambda name: (name == CANDIDATE, name))
    for name in missing:
        require(packages.sha256(directory / name) == expected[name], f"{name} changed before its upload.")
        run(["gh", "release", "upload", tag, str(directory / name), "--repo", repository])

    return {"tag": tag, "uploaded": missing}


def download_candidate(api, repository: str, tag: str, output: Path) -> list[str]:
    """Download every asset of the tag's release, including a draft, by release ID."""
    release = release_for(api, repository, tag)
    require(release is not None, f"No release or draft for {tag}.")
    output.mkdir(parents=True, exist_ok=True)
    names = []
    for name, asset in sorted(release_assets(api, repository, release).items()):
        require(re.fullmatch(r"[A-Za-z0-9._+-]+", name) is not None, f"Unexpected asset name {name!r}.")
        (output / name).write_bytes(asset_bytes(repository, asset))
        names.append(name)

    return names


def verify_candidate(repository: str, directory: Path, *, verify=attestation.verify) -> dict:
    """Verify the downloaded candidate assets byte for byte and against their attestations."""
    manifest = read_json(directory / CANDIDATE)
    require(manifest.get("schemaVersion") == 1 and manifest.get("repository") == repository,
            "The candidate manifest belongs to another repository.")
    expected = expected_assets(manifest)
    present = {path.name for path in directory.iterdir() if path.is_file()} - {CANDIDATE}
    # WHY: The draft stays editable until it is published; exactly the expected assets with exactly their digests
    # may be published (MySql, SafeMigrations and NestedSet).
    require(present == set(expected), "The candidate assets differ from the manifest: "
                                      f"{', '.join(sorted(present ^ set(expected)))}.")
    for name, digest in expected.items():
        require(not (directory / name).is_symlink() and packages.sha256(directory / name) == digest,
                f"{name} differs from the candidate manifest.")

    require(read_json(directory / PROBES).get("probes") == manifest["symbols"], "The symbol probes differ.")
    nuget.validate_symbol_probes(manifest["symbols"])
    source = {"source_digest": manifest["commit"], "source_ref": manifest["sourceRef"]}
    # WHY: The provenance must cover every package with its digest and come from the candidate's commit and line
    # (MySql, SafeMigrations and NestedSet); only the release-candidate workflow of this repository signs it.
    provenance = verify(directory / CANDIDATE, repository, attestation.SLSA_PROVENANCE, **source)
    origin = attestation.latest(provenance)
    subjects = origin["subjects"]
    for package in manifest["packages"]:
        for kind in ("nupkg", "snupkg"):
            require(subjects.get(package[kind]) == package[f"{kind}Sha256"],
                    f"The provenance does not cover {package[kind]}.")

    # WHY: Each SBOM attestation must carry exactly the SBOM of the candidate (NestedSet).
    for name, sbom in manifest["sboms"].items():
        attested = verify(directory / sbom["package"], repository, sbom["predicateType"],
                          signer=attestation.SIGNERS[attestation.SLSA_PROVENANCE], **source)
        require(any(item["predicate"] == read_json(directory / name) for item in attested),
                f"No SBOM attestation of {sbom['package']} equals {name}.")

    # WHY: A second dispatch can qualify the same commit and version without permitting publication. Use the
    # signed run identity of the candidate, allowing earlier attempts because re-runs reuse successful jobs.
    # https://docs.github.com/en/actions/concepts/security/artifact-attestations
    # https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs
    with tempfile.TemporaryDirectory(prefix="candidate-subject-") as subject_directory:
        subject = attestation.tree_subject(Path(subject_directory), manifest["tree"])
        qualified = [item for item in verify(subject, repository, attestation.RELEASE_QUALIFICATION, **source)
                     if item["runId"] == origin["runId"] and item["attempt"] <= origin["attempt"]
                     and item["predicate"].get("commit") == manifest["commit"]
                     and item["predicate"].get("version") == manifest["version"]]

    require(bool(qualified), f"No release qualification from candidate run {origin['runId']} for "
                             f"{manifest['commit']} and {manifest['version']}.")
    manifest["qualification"] = attestation.latest(qualified)["predicate"]
    require(manifest["qualification"]["snapshot"].get("qualificationOnly") is False,
            "A qualification-only candidate is never published.")
    return manifest


def verify_signed_tag(api, root: Path, manifest: dict) -> str:
    """Require an annotated SSH-signed tag on the candidate commit, verified locally and by GitHub (T1-T5, T7)."""
    repository, tag, commit = manifest["repository"], manifest["tag"], manifest["commit"]
    branch = manifest["sourceRef"].removeprefix("refs/heads/")
    run(["git", "-C", str(root), "fetch", "--no-tags", "origin", f"+refs/tags/{tag}:refs/tags/{tag}",
         f"+refs/heads/{branch}:refs/remotes/origin/{branch}"])
    require(not run(["git", "-C", str(root), "status", "--porcelain"]).strip(), "The checkout is not clean.")
    require(run(["git", "-C", str(root), "cat-file", "-t", f"refs/tags/{tag}"]).strip() == "tag",
            f"{tag} must be an annotated tag.")
    require(run(["git", "-C", str(root), "rev-parse", f"refs/tags/{tag}^{{commit}}"]).strip() == commit,
            f"{tag} does not point to the candidate commit.")
    # WHY: The signer list comes from the candidate commit, the reviewed source that was qualified (NestedSet reads
    # it from the qualified checkout), not from whatever the branch holds now.
    signers = run(["git", "-C", str(root), "show", f"{commit}:{ALLOWED_SIGNERS}"])
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".allowed_signers", delete=False) as stream:
        stream.write(signers)

    # WHY: git verify-tag checks SSH signatures against gpg.ssh.allowedSignersFile.
    # https://git-scm.com/docs/git-config#Documentation/git-config.txt-gpgsshallowedSignersFile
    run(["git", "-C", str(root), "-c", "gpg.format=ssh", "-c", f"gpg.ssh.allowedSignersFile={stream.name}",
         "verify-tag", tag])
    local = run(["git", "-C", str(root), "rev-parse", f"refs/tags/{tag}"]).strip()
    reference = api.request(f"repos/{repository}/git/ref/tags/{quote(tag, safe='')}")
    require(reference.get("object", {}).get("type") == "tag" and reference["object"].get("sha") == local,
            f"{tag} on GitHub is not the tag object verified locally.")
    remote = api.request(f"repos/{repository}/git/tags/{local}")
    verification = remote.get("verification", {})
    require(remote.get("object", {}).get("sha") == commit and verification.get("verified") is True
            and verification.get("reason") == "valid", f"GitHub does not verify the signature of {tag}.")
    # WHY: The commit must belong to the protected line, not only be a commit someone tagged. This needs the full
    # history, so the publish job checks out with fetch-depth 0.
    ancestor = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", commit, f"origin/{branch}"],
                              capture_output=True, timeout=60, check=False)
    require(ancestor.returncode == 0, f"{commit} is not part of {manifest['sourceRef']}.")
    return local


def verify_publisher_environment(api, repository: str, policy: dict, environment_name: str) -> None:
    """Require reviewers and exactly the active lines as branch grants, each line protected.

    The environment rule of MySql, SafeMigrations and NestedSet.
    """
    require(environment_name == policy["release"]["environment"],
            f"The publish job runs in '{environment_name}', not in {policy['release']['environment']}.")
    base = f"repos/{repository}/environments/{quote(environment_name, safe='')}"
    value = api.request(base)
    branch_policy = value.get("deployment_branch_policy") or {}
    require(any(rule.get("type") == "required_reviewers" and rule.get("reviewers")
                for rule in value.get("protection_rules") or []),
            f"Environment {environment_name} has no required reviewers.")
    require(branch_policy.get("custom_branch_policies") is True and branch_policy.get("protected_branches") is False,
            f"Environment {environment_name} must grant selected branches only.")
    grants, total = [], None
    for page in range(1, 101):
        response = api.request(f"{base}/deployment-branch-policies?per_page=100&page={page}")
        entries = response.get("branch_policies") if isinstance(response, dict) else None
        require(isinstance(entries, list) and type(response.get("total_count")) is int,
                f"Environment {environment_name} has an invalid grant listing.")
        total = response["total_count"]
        grants.extend(entries)
        if len(entries) < 100:
            break

    require(len(grants) == total and all(grant.get("type") == "branch" and isinstance(grant.get("name"), str)
                                         and not WILDCARD.search(grant["name"]) for grant in grants),
            f"Environment {environment_name} must grant exact branch names only.")
    active = {line["sourceRef"] for line in policies.lines_for(policy, "publication")}
    require({f"refs/heads/{grant['name']}" for grant in grants} == active,
            f"Environment {environment_name} grants other branches than the active lines.")
    for source_ref in sorted(active):
        protected_head(api, repository, source_ref)


def authorize(api, environment: dict, root: Path, directory: Path, *, verify=attestation.verify) -> dict:
    """Recheck candidate bytes and attestations, authority, environment and signed tag; no writes."""
    repository = environment["GITHUB_REPOSITORY"]
    manifest = verify_candidate(repository, directory, verify=verify)
    authority = observe_authority(api, repository)
    policy = authority["policy"]
    require_release_workflow(policy, environment)
    policies.select_line(policy, manifest["sourceRef"], manifest["version"], purpose="publication")
    verify_publisher_environment(api, repository, policy, environment.get("PUBLISHER_ENVIRONMENT", ""))
    manifest["tagObject"] = verify_signed_tag(api, root, manifest)
    manifest["authorityCommit"] = authority["commit"]
    # WHY: Earlier admissions, from earlier attempts or runs, must name the same candidate and tag object; a
    # different one would mean two candidates for one version (MySql, SafeMigrations and NestedSet).
    earlier = (verify(directory / CANDIDATE, repository, attestation.RELEASE_ADMISSION)
               if attestation.exists(api, repository, directory / CANDIDATE, attestation.RELEASE_ADMISSION) else [])
    require_same_admissions(earlier, manifest)
    return manifest


def require_same_admissions(earlier: list[dict], manifest: dict) -> None:
    """Require every earlier admission of the version to name this tag object and candidate."""
    for item in earlier:
        require(all(item["predicate"].get(key) == manifest[key] for key in ("tagObject", "commit", "tree", "version")),
                "An earlier admission names another tag object or candidate for this version.")


def admission(manifest: dict, environment: dict) -> dict:
    """Return the predicate that records which candidate this run admitted, before any irreversible write."""
    return {"schemaVersion": 1, "repository": manifest["repository"], "version": manifest["version"],
            "tag": manifest["tag"], "tagObject": manifest["tagObject"], "commit": manifest["commit"],
            "tree": manifest["tree"], "authorityCommit": manifest["authorityCommit"],
            "runId": int(environment["GITHUB_RUN_ID"]), "attempt": int(environment["GITHUB_RUN_ATTEMPT"])}


def verified_tag_commit(api, repository: str, tag: str) -> str:
    """Return the commit of an annotated tag whose signature GitHub verifies."""
    reference = api.request(f"repos/{repository}/git/ref/tags/{quote(tag, safe='')}")
    require(reference.get("object", {}).get("type") == "tag", f"{tag} is not an annotated tag.")
    remote = api.request(f"repos/{repository}/git/tags/{reference['object']['sha']}")
    verification = remote.get("verification", {})
    require(verification.get("verified") is True and verification.get("reason") == "valid"
            and remote.get("object", {}).get("type") == "commit", f"GitHub does not verify the tag {tag}.")
    return remote["object"]["sha"]


def latest_flag(api, repository: str, version: str) -> bool:
    """Mark a release Latest only when it is a stable version strictly newer than the current Latest.

    The Latest rule of MySql, SafeMigrations and NestedSet.
    """
    if policies.parse_version(version).is_prerelease:
        return False

    try:
        current = api.request(f"repos/{repository}/releases/latest")
    except GitHubError as error:
        if error.code == "github_http_error" and error.http_status == 404:
            return True

        raise

    tag = current.get("tag_name", "")
    # WHY: The current Latest must be a complete, immutable, stable release with a verified annotated tag before a
    # comparison with it means anything (all three repositories).
    require(tag.startswith("v") and current.get("immutable") is True and current.get("draft") is False
            and current.get("prerelease") is False and not policies.parse_version(tag[1:]).is_prerelease,
            "The current Latest release is not an immutable stable release.")
    verified_tag_commit(api, repository, tag)
    # WHY: Without an explicit flag GitHub makes "the most recent non-prerelease, non-draft release, sorted by the
    # created_at attribute" Latest, so a maintenance release published later would replace a higher version.
    # https://docs.github.com/en/rest/releases/releases#get-the-latest-release
    return policies.compare_versions(version, tag[1:]) > 0


def check_publish_target(api, repository: str, manifest: dict, directory: Path) -> None:
    """Check the destination before NuGet receives any package."""
    release = release_for(api, repository, manifest["tag"])
    require(release is not None, f"The draft of {manifest['tag']} is missing.")
    require_release_metadata(release, manifest, (directory / NOTES).read_text(encoding="utf-8"))
    if release.get("draft"):
        # WHY: An existing Latest can predate the repository's immutability setting. Resolve that policy check
        # before pushing any NuGet version, and resolve it again immediately before publication.
        # https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/establish-provenance-and-integrity/prevent-release-changes
        # https://learn.microsoft.com/en-us/nuget/nuget-org/policies/deleting-packages
        latest_flag(api, repository, manifest["version"])
    else:
        require(release.get("immutable") is True, f"The published release of {manifest['tag']} is not immutable.")

    expected = {**expected_assets(manifest), CANDIDATE: packages.sha256(directory / CANDIDATE)}
    problems = draft_asset_problems(repository, release_assets(api, repository, release), expected)
    require(not problems, f"The release of {manifest['tag']} changed since verification: {', '.join(problems)}.")


def publish_release(api, repository: str, manifest: dict, directory: Path, *, attempts: int = 30,
                    sleep=time.sleep) -> dict:
    """Publish the draft, then require GitHub to report it immutable and to verify the release and every asset."""
    release = release_for(api, repository, manifest["tag"])
    require(release is not None, f"The draft of {manifest['tag']} is missing.")
    require_release_metadata(release, manifest, (directory / NOTES).read_text(encoding="utf-8"))
    promote = False
    expected = {**expected_assets(manifest), CANDIDATE: packages.sha256(directory / CANDIDATE)}
    if release.get("draft"):
        # WHY: The draft stays editable until it is published, and publishing an immutable release locks the assets
        # the draft holds at that moment, not the verified copy of this job. The current assets are compared right
        # before the edit; GitHub has no conditional edit, so verify-asset below still checks the result.
        # https://docs.github.com/en/code-security/supply-chain-security/understanding-your-software-supply-chain/immutable-releases
        # The Latest decision reads other releases, so it comes first and nothing lies between comparison and edit.
        promote = latest_flag(api, repository, manifest["version"])
        problems = draft_asset_problems(repository, release_assets(api, repository, release), expected)
        require(not problems, f"The draft of {manifest['tag']} changed since its verification: {', '.join(problems)}.")
        # WHY: The release is edited by its numeric ID; a tag-based edit "could address a replacement release
        # between inspection and mutation" (MySql publish_existing_release). make_latest "Defaults to true for
        # newly published releases", so it is always set explicitly. https://docs.github.com/en/rest/releases/releases
        api.request(f"repos/{repository}/releases/{release['id']}", "PATCH", {
            "draft": False, "prerelease": policies.parse_version(manifest["version"]).is_prerelease,
            "make_latest": "true" if promote else "false"})

    files = [directory / name for name in sorted({*expected_assets(manifest), CANDIDATE})]
    # WHY: Publishing an immutable release "automatically generates a release attestation"; it can appear after
    # the release itself, so verification is retried within a fixed budget (NestedSet's verify_immutable).
    # https://docs.github.com/en/code-security/supply-chain-security/understanding-your-software-supply-chain/immutable-releases
    for attempt in range(attempts):
        release = release_for(api, repository, manifest["tag"])
        if release and release.get("draft") is False and release.get("immutable") is True:
            try:
                run(["gh", "release", "verify", manifest["tag"], "--repo", repository])
                for path in files:
                    run(["gh", "release", "verify-asset", manifest["tag"], str(path), "--repo", repository])

                # WHY: verify-asset checks the named files only; an extra asset in the published release is found
                # by comparing the whole inventory, which an immutable release can no longer change.
                problems = draft_asset_problems(repository, release_assets(api, repository, release), expected)
                if problems:
                    raise ReleaseInventoryError(f"The release {manifest['tag']} holds other assets than the "
                                                f"candidate: {', '.join(problems)}.")

                break
            except ReleaseInventoryError:
                raise
            except PublicationError:
                pass

        require(attempt + 1 < attempts, f"{manifest['tag']} did not become an immutable, verified release in time.")
        sleep(10)

    if promote:
        latest = api.request(f"repos/{repository}/releases/latest")
        require(latest.get("tag_name") == manifest["tag"], f"GitHub did not make {manifest['tag']} Latest.")

    return release


def repository_of(root: Path) -> str:
    """Return owner/name of the checkout's origin on GitHub."""
    url = run(["git", "-C", str(root), "remote", "get-url", "origin"]).strip()
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?", url)
    require(match is not None, "origin is not a GitHub repository.")
    return match[1]


def signing_key_registered(root: Path, signing_key: str) -> bool:
    """Return whether the configured SSH signing key's public key is listed in .github/allowed_signers."""
    if signing_key.startswith("key::"):
        public = signing_key.removeprefix("key::")
    elif signing_key.startswith("ssh-"):
        public = signing_key
    else:
        path = Path(signing_key).expanduser()
        public_path = path if path.suffix == ".pub" else path.with_name(path.name + ".pub")
        require(public_path.is_file(), f"The public key of the signing key ({public_path}) does not exist.")
        public = public_path.read_text(encoding="utf-8")

    key_type, _, key_data = public.strip().partition(" ")
    key = f"{key_type} {key_data.split()[0]}" if key_data else ""
    signers = (root / ALLOWED_SIGNERS).read_text(encoding="utf-8").splitlines()
    return bool(key) and any(key in line for line in signers if line.strip() and not line.lstrip().startswith("#"))


def require_immutable_releases(api, repository: str) -> None:
    """Check the repository setting before any publication write."""
    # WHY: Publishing locks the release only when immutable releases are enabled; the job token cannot check it,
    # because "GET /repos/{owner}/{repo}/immutable-releases" needs the Administration permission, which a
    # GITHUB_TOKEN never has. The documentation names 404 for a disabled setting, but
    # GitHub also answers 200 with "enabled": false (doka-labs/workflows; NestedSet answers "enabled": true).
    # https://docs.github.com/en/rest/repos/repos#check-if-immutable-releases-are-enabled-for-a-repository
    # https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps
    try:
        setting = api.request(f"repos/{repository}/immutable-releases")
    except GitHubError as error:
        if error.code != "github_http_error" or error.http_status != 404:
            raise

        setting = {"enabled": False}

    require(isinstance(setting, dict) and setting.get("enabled") is True,
            f"Immutable releases are not enabled for {repository}, or the token cannot read the setting.")


def pre_tag(api, root: Path, version: str, *, fetcher=nuget.fetch, verify=attestation.verify) -> dict:
    """Check locally, without any write, that the checkout can be tagged for the version (NestedSet's pre_tag)."""
    repository = repository_of(root)
    release_notes(root, version)
    require_immutable_releases(api, repository)

    require(not run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"]).strip(),
            "The checkout is not clean.")
    branch = run(["git", "-C", str(root), "branch", "--show-current"]).strip()
    source_ref = f"refs/heads/{branch}"
    policy = observe_authority(api, repository)["policy"]
    policies.select_line(policy, source_ref, version, purpose="publication")
    commit = run(["git", "-C", str(root), "rev-parse", "HEAD"]).strip()
    require(protected_head(api, repository, source_ref) == commit,
            f"The checkout is not the current head of the protected {branch}.")
    tag = f"v{version}"
    require(not tag_exists(api, repository, tag), f"{tag} already exists.")
    others = tags_on_commit(root, commit)
    require(not others, f"{commit} already carries the tags {', '.join(others)}.")
    taken = sorted(name for name, state in nuget.unused(policy["release"]["packages"], version, fetcher=fetcher).items()
                   if state != "absent")
    require(not taken, f"{version} is already on nuget.org for {', '.join(taken)}.")
    release = release_for(api, repository, tag)
    require(release is not None and release.get("draft") is True, f"No draft release holds a candidate for {tag}.")
    # WHY: The tag must point at the commit the draft's candidate was built from (NestedSet).
    asset = release_assets(api, repository, release).get(CANDIDATE)
    require(asset is not None, f"The draft of {tag} has no {CANDIDATE}.")
    manifest = json.loads(asset_bytes(repository, asset))

    require(manifest.get("commit") == commit and manifest.get("version") == version,
            f"The draft's candidate was built from {manifest.get('commit')}, not from {commit}.")
    # WHY: Once pushed, a tag blocks a new candidate for its version (preflight refuses a tag without a release), so
    # the draft must already pass the verification the publish job runs: every asset, the provenance and exactly
    # the SBOMs attested. A draft that fails here is deleted and rebuilt before anything is tagged.
    require_complete_draft(api, repository, release, verify=verify)
    # WHY: Tags are SSH-signed; git verify-tag later checks them against .github/allowed_signers.
    require(run(["git", "-C", str(root), "config", "--get", "gpg.format"]).strip() == "ssh",
            "Configure SSH signing (git config gpg.format ssh) before tagging.")
    signing_key = subprocess.run(["git", "-C", str(root), "config", "--get", "user.signingkey"], capture_output=True,
                                 text=True, timeout=30, check=False).stdout.strip()
    require(bool(signing_key), "Configure the signing key (git config user.signingkey) before tagging.")
    require(signing_key_registered(root, signing_key), f"The signing key is not listed in {ALLOWED_SIGNERS}.")
    return {"tag": tag, "commit": commit, "command": f'git tag -s {tag} {commit} -m "{tag}"'}
