import json
import tempfile
import unittest
import unittest.mock
import zipfile
from pathlib import Path
from urllib.parse import unquote

from tools.release_lines import attestation
from tools.release_lines.github import GitHubError
from tools.release_publication import nuget, packages, publication

REPOSITORY = "doka-labs/Example"
COMMIT = "a" * 40


def nuspec(package_id, version, *, commit=COMMIT, dependencies=(), symbols=False, readme=True):
    deps = "".join(f'<dependency id="{item}" version="[{version}]" />' for item in dependencies)
    package_type = '<packageTypes><packageType name="SymbolsPackage" /></packageTypes>' if symbols else ""
    return (f'<?xml version="1.0"?><package xmlns="http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd">'
            f"<metadata><id>{package_id}</id><version>{version}</version>"
            f'<license type="expression">MIT</license>{"<readme>README.md</readme>" if readme else ""}'
            f'<repository type="git" url="https://github.com/{REPOSITORY}" commit="{commit}" />{package_type}'
            f'<dependencies><group targetFramework="net10.0">{deps}</group></dependencies></metadata></package>')


def write_package(directory, package_id, version="1.2.3", **options):
    for suffix, symbols in (("nupkg", False), ("snupkg", True)):
        with zipfile.ZipFile(directory / f"{package_id}.{version}.{suffix}", "w") as archive:
            archive.writestr(f"{package_id}.nuspec", nuspec(package_id, version, symbols=symbols, **options))
            archive.writestr(f"lib/net10.0/{package_id}.{'pdb' if symbols else 'dll'}", b"payload")


class ReleaseNotesTests(unittest.TestCase):
    def notes(self, text, version="1.2.3"):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "CHANGELOG.md").write_text(text, encoding="utf-8")
            return publication.release_notes(Path(directory), version)

    def test_keep_a_changelog_heading_passes(self):
        # Arrange
        text = "# Changelog\n\n## [Unreleased]\n\n## [1.2.3] - 2026-11-22\n\n- Fixed it.\n\n## [1.2.2] - 2026-11-01\n"

        # Act
        notes = self.notes(text)

        # Assert
        self.assertEqual("# 1.2.3\n\n- Fixed it.\n", notes)

    def test_parenthesized_date_heading_passes(self):
        # Arrange
        text = "## 1.2.3 (2026-11-22)\n\nNotes.\n"

        # Act
        notes = self.notes(text)

        # Assert
        self.assertEqual("# 1.2.3\n\nNotes.\n", notes)

    def test_missing_section_fails(self):
        # Arrange
        text = "## [1.2.2] - 2026-11-01\n\n- Old.\n"

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            self.notes(text)

        # Assert
        self.assertIn("exactly one dated section for 1.2.3", str(raised.exception))

    def test_empty_section_fails(self):
        # Arrange
        text = "## [1.2.3] - 2026-11-22\n\n## [1.2.2] - 2026-11-01\n- Old.\n"

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            self.notes(text)

        # Assert
        self.assertIn("no notes", str(raised.exception))


class PackageInventoryTests(unittest.TestCase):
    def inventory(self, setup, commit=COMMIT):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            setup(root)
            return packages.inventory(root, "1.2.3", REPOSITORY, commit, ["net10.0"])

    def test_packages_are_ordered_by_dependency(self):
        # Arrange
        def setup(root):
            write_package(root, "Doka.Adapter", dependencies=["Doka.Core"])
            write_package(root, "Doka.Core")

        # Act
        inventory = self.inventory(setup)

        # Assert
        self.assertEqual(["Doka.Core", "Doka.Adapter"], [item["id"] for item in inventory])

    def test_package_of_other_commit_fails(self):
        # Arrange
        def setup(root):
            write_package(root, "Doka.Core", commit="b" * 40)

        # Act
        with self.assertRaises(packages.PackageError) as raised:
            self.inventory(setup)

        # Assert
        self.assertIn(f"is not bound to {REPOSITORY} at {COMMIT}", str(raised.exception))

    def test_missing_symbol_package_fails(self):
        # Arrange
        def setup(root):
            write_package(root, "Doka.Core")
            (root / "Doka.Core.1.2.3.snupkg").unlink()

        # Act
        with self.assertRaises(packages.PackageError) as raised:
            self.inventory(setup)

        # Assert
        self.assertIn("matching symbol package", str(raised.exception))

    def test_package_without_readme_fails(self):
        # Arrange
        def setup(root):
            write_package(root, "Doka.Core", readme=False)

        # Act
        with self.assertRaises(packages.PackageError) as raised:
            self.inventory(setup)

        # Assert
        self.assertIn("declares no readme", str(raised.exception))

    def test_unsafe_archive_path_fails(self):
        # Arrange
        def setup(root):
            write_package(root, "Doka.Core")
            with zipfile.ZipFile(root / "Doka.Core.1.2.3.nupkg", "a") as archive:
                archive.writestr("../escape.txt", b"x")

        # Act
        with self.assertRaises(packages.PackageError) as raised:
            self.inventory(setup)

        # Assert
        self.assertIn("Unsafe archive path", str(raised.exception))

    def test_dependency_cycle_fails(self):
        # Arrange
        def setup(root):
            write_package(root, "Doka.A", dependencies=["Doka.B"])
            write_package(root, "Doka.B", dependencies=["Doka.A"])

        # Act
        with self.assertRaises(packages.PackageError) as raised:
            self.inventory(setup)

        # Assert
        self.assertIn("cycle", str(raised.exception))


class FakeFetcher:
    def __init__(self, responses):
        self.responses = responses

    def __call__(self, url, headers, timeout):
        if url == nuget.INDEX:
            return 200, json.dumps({"resources": [{"@type": "PackageBaseAddress/3.0.0",
                                                   "@id": "https://api.nuget.org/v3-flatcontainer/"}]}).encode()

        value = self.responses.get(url, (404, b""))
        return value.pop(0) if isinstance(value, list) else value


class NuGetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        write_package(self.root, "Doka.Core")
        self.inventory = packages.inventory(self.root, "1.2.3", REPOSITORY, COMMIT, ["net10.0"])
        self.url = "https://api.nuget.org/v3-flatcontainer/doka.core/1.2.3/doka.core.1.2.3.nupkg"

    def tearDown(self):
        self.directory.cleanup()

    def signed(self, payload_change=False):
        target = self.root / "signed.nupkg"
        with zipfile.ZipFile(self.root / "Doka.Core.1.2.3.nupkg") as source, zipfile.ZipFile(target, "w") as archive:
            for entry in source.infolist():
                data = source.read(entry)
                archive.writestr(entry.filename, data + (b"!" if payload_change and entry.filename.endswith(".dll")
                                                         else b""))

            archive.writestr(packages.SIGNATURE_ENTRY, b"signature")

        return target.read_bytes()

    def test_absent_package_is_reported_absent(self):
        # Arrange
        fetcher = FakeFetcher({})

        # Act
        states = nuget.availability(self.root, self.inventory, fetcher=fetcher)

        # Assert
        self.assertEqual([{"id": "Doka.Core", "state": "absent"}], states)

    def test_signed_identical_package_is_matching(self):
        # Arrange
        fetcher = FakeFetcher({self.url: (200, self.signed())})

        # Act
        states = nuget.availability(self.root, self.inventory, fetcher=fetcher)

        # Assert
        self.assertEqual("matching", states[0]["state"])

    def test_other_bytes_on_nuget_fail(self):
        # Arrange
        fetcher = FakeFetcher({self.url: (200, self.signed(payload_change=True))})

        # Act
        with self.assertRaises(nuget.NuGetError) as raised:
            nuget.availability(self.root, self.inventory, fetcher=fetcher)

        # Assert
        self.assertIn("other bytes", str(raised.exception))

    def test_unclear_answer_fails(self):
        # Arrange
        fetcher = FakeFetcher({self.url: (0, b"")})

        # Act
        with self.assertRaises(nuget.NuGetError) as raised:
            nuget.availability(self.root, self.inventory, fetcher=fetcher)

        # Assert
        self.assertIn("Cannot establish", str(raised.exception))

    def test_readback_waits_for_signature_and_symbols(self):
        # Arrange
        probe = {"url": "https://symbols.nuget.org/download/symbols/x.pdb/k/x.pdb", "checksum": "SHA256:00",
                 "pdb": "lib/net10.0/Doka.Core.pdb", "sha256": __import__("hashlib").sha256(b"pdb").hexdigest()}
        unsigned = (self.root / "Doka.Core.1.2.3.nupkg").read_bytes()
        fetcher = FakeFetcher({self.url: [(404, b""), (200, unsigned), (200, self.signed())],
                               probe["url"]: [(404, b""), (200, b"pdb")]})
        now = [0.0]

        # Act
        result = nuget.readback(self.root, self.inventory, [probe], self.root / "work", fetcher=fetcher,
                                verifier=lambda path, timeout: None, clock=lambda: now[0],
                                sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))

        # Assert
        self.assertEqual(["Doka.Core"], [item["id"] for item in result["packages"]])
        self.assertEqual([probe["url"]], result["symbols"])

    def test_readback_deadline_is_pending_not_failure(self):
        # Arrange
        fetcher = FakeFetcher({})
        now = [0.0]

        # Act
        with self.assertRaises(nuget.PendingError) as raised:
            nuget.readback(self.root, self.inventory, [], self.root / "work", fetcher=fetcher,
                           verifier=lambda path, timeout: None, clock=lambda: now[0],
                           sleep=lambda seconds: now.__setitem__(0, now[0] + seconds), timeout=30)

        # Assert
        self.assertIn("before the deadline", str(raised.exception))


class FakeApi:
    def __init__(self, routes):
        self.routes = routes

    def request(self, route, method="GET", payload=None):
        value = self.routes.get(route) if route in self.routes else self.routes.get(route.split("?", 1)[0])
        if isinstance(value, GitHubError):
            raise value

        if value is None:
            raise GitHubError("missing", code="github_http_error", http_status=404)

        return value


def verified_tag(tag, commit=COMMIT):
    return {f"repos/{REPOSITORY}/git/ref/tags/{tag}": {"object": {"type": "tag", "sha": "d" * 40}},
            f"repos/{REPOSITORY}/git/tags/{'d' * 40}": {"object": {"type": "commit", "sha": commit},
                                                        "verification": {"verified": True, "reason": "valid"}}}


class LatestTests(unittest.TestCase):
    def latest(self, version, current=None, tag_routes=None):
        routes = dict(tag_routes or {})
        if current is not None:
            routes[f"repos/{REPOSITORY}/releases/latest"] = current

        return publication.latest_flag(FakeApi(routes), REPOSITORY, version)

    def stable(self, tag):
        return {"tag_name": tag, "immutable": True, "draft": False, "prerelease": False}

    def test_older_maintenance_release_is_not_latest(self):
        # Arrange
        current = self.stable("v11.0.0")

        # Act
        latest = self.latest("10.4.1", current, verified_tag("v11.0.0"))

        # Assert
        self.assertFalse(latest)

    def test_newer_stable_release_is_latest(self):
        # Arrange
        current = self.stable("v11.0.0")

        # Act
        latest = self.latest("11.0.1", current, verified_tag("v11.0.0"))

        # Assert
        self.assertTrue(latest)

    def test_mutable_current_latest_fails(self):
        # Arrange
        current = {**self.stable("v11.0.0"), "immutable": False}

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            self.latest("11.0.1", current, verified_tag("v11.0.0"))

        # Assert
        self.assertIn("not an immutable stable release", str(raised.exception))

    def test_current_latest_with_unverified_tag_fails(self):
        # Arrange
        routes = verified_tag("v11.0.0")
        routes[f"repos/{REPOSITORY}/git/tags/{'d' * 40}"]["verification"] = {"verified": False, "reason": "unsigned"}

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            self.latest("11.0.1", self.stable("v11.0.0"), routes)

        # Assert
        self.assertIn("does not verify the tag v11.0.0", str(raised.exception))

    def test_prerelease_is_never_latest(self):
        # Arrange
        version = "12.0.0-rc.1"

        # Act
        latest = self.latest(version)

        # Assert
        self.assertFalse(latest)

    def test_first_release_is_latest(self):
        # Arrange
        version = "1.0.0"

        # Act
        latest = self.latest(version)

        # Assert
        self.assertTrue(latest)


SBOM = {"spdxVersion": "SPDX-2.3", "name": "Doka.Core"}


def candidate_directory(root):
    write_package(root, "Doka.Core")
    inventory = packages.inventory(root, "1.2.3", REPOSITORY, COMMIT, ["net10.0"])
    (root / "Doka.Core.spdx.json").write_text(json.dumps(SBOM), encoding="ascii")
    probes = [{"package": "Doka.Core.1.2.3.nupkg", "url": "u", "checksum": "c", "pdb": "p", "sha256": "s"}]
    (root / publication.PROBES).write_text(json.dumps({"schemaVersion": 1, "probes": probes}), encoding="ascii")
    (root / publication.NOTES).write_text("# 1.2.3\n\nNotes.\n", encoding="utf-8")
    manifest = {"schemaVersion": 1, "repository": REPOSITORY, "version": "1.2.3", "tag": "v1.2.3",
                "sourceRef": "refs/heads/main", "commit": COMMIT, "tree": "c" * 40, "packages": inventory,
                "symbols": probes,
                "sboms": {"Doka.Core.spdx.json": {"sha256": packages.sha256(root / "Doka.Core.spdx.json"),
                                                  "package": "Doka.Core.1.2.3.nupkg",
                                                  "predicateType": "https://spdx.dev/Document/v2.3"}},
                "notesSha256": packages.sha256(root / publication.NOTES),
                "probesSha256": packages.sha256(root / publication.PROBES)}
    (root / publication.CANDIDATE).write_text(json.dumps(manifest), encoding="ascii")
    return manifest


class VerifyCandidateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.manifest = candidate_directory(self.root)
        self.calls = []

    def tearDown(self):
        self.directory.cleanup()

    def fake(self, *, qualification=None, subjects=None, sbom=SBOM):
        package = self.manifest["packages"][0]
        subjects = subjects if subjects is not None else {package["nupkg"]: package["nupkgSha256"],
                                                          package["snupkg"]: package["snupkgSha256"]}
        qualification = qualification if qualification is not None else [
            {"predicate": {"commit": COMMIT, "version": "1.2.3", "snapshot": {"qualificationOnly": False}},
             "runId": 5, "attempt": 1, "subjects": {}}]

        def verify(subject, repository, predicate_type, **options):
            self.calls.append((predicate_type, options))
            if predicate_type == attestation.SLSA_PROVENANCE:
                return [{"predicate": {}, "runId": 1, "attempt": 1, "subjects": subjects}]

            if predicate_type.startswith("https://spdx.dev/"):
                return [{"predicate": sbom, "runId": 1, "attempt": 1, "subjects": {}}]

            return qualification

        return verify

    def test_attested_candidate_passes(self):
        # Arrange
        fake = self.fake()

        # Act
        manifest = publication.verify_candidate(REPOSITORY, self.root, verify=fake)

        # Assert
        self.assertEqual(COMMIT, manifest["qualification"]["commit"])
        self.assertTrue(all(options.get("source_digest") == COMMIT and options.get("source_ref") == "refs/heads/main"
                            for _, options in self.calls))

    def test_replaced_package_fails(self):
        # Arrange
        with zipfile.ZipFile(self.root / "Doka.Core.1.2.3.nupkg", "a") as archive:
            archive.writestr("extra.txt", b"x")

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            publication.verify_candidate(REPOSITORY, self.root, verify=self.fake())

        # Assert
        self.assertIn("differs from the candidate manifest", str(raised.exception))

    def test_unexpected_asset_fails(self):
        # Arrange
        (self.root / "extra.dll").write_bytes(b"x")

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            publication.verify_candidate(REPOSITORY, self.root, verify=self.fake())

        # Assert
        self.assertIn("extra.dll", str(raised.exception))

    def test_provenance_without_symbol_package_fails(self):
        # Arrange
        package = self.manifest["packages"][0]
        fake = self.fake(subjects={package["nupkg"]: package["nupkgSha256"]})

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            publication.verify_candidate(REPOSITORY, self.root, verify=fake)

        # Assert
        self.assertIn("does not cover Doka.Core.1.2.3.snupkg", str(raised.exception))

    def test_sbom_attestation_of_other_document_fails(self):
        # Arrange
        fake = self.fake(sbom={"spdxVersion": "SPDX-2.3", "name": "Other"})

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            publication.verify_candidate(REPOSITORY, self.root, verify=fake)

        # Assert
        self.assertIn("equals Doka.Core.spdx.json", str(raised.exception))

    def test_candidate_without_matching_qualification_fails(self):
        # Arrange
        fake = self.fake(qualification=[{"predicate": {"commit": "b" * 40, "version": "1.2.3"}, "runId": 5,
                                         "attempt": 1, "subjects": {}}])

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            publication.verify_candidate(REPOSITORY, self.root, verify=fake)

        # Assert
        self.assertIn("No release qualification", str(raised.exception))

    def test_duplicate_manifest_key_fails(self):
        # Arrange
        text = (self.root / publication.CANDIDATE).read_text(encoding="ascii")
        (self.root / publication.CANDIDATE).write_text(text.replace('{"schemaVersion": 1,', '{"schemaVersion": 1, "schemaVersion": 1,', 1), encoding="ascii")

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            publication.verify_candidate(REPOSITORY, self.root, verify=self.fake())

        # Assert
        self.assertIn("repeats the key schemaVersion", str(raised.exception))


class EnvironmentTests(unittest.TestCase):
    def check(self, grants, *, reviewers=True):
        policy = json.loads((Path(__file__).resolve().parents[3] / "examples" / "release-policy.json").read_text())
        base = f"repos/{policy['repository']}/environments/nuget"
        routes = {base: {"protection_rules": [{"type": "required_reviewers", "reviewers": [{"id": 1}] if reviewers else []}],
                         "deployment_branch_policy": {"custom_branch_policies": True, "protected_branches": False}},
                  f"{base}/deployment-branch-policies": {"total_count": len(grants), "branch_policies": grants}}
        for branch in ("main", "maintenance%2F10.x"):
            routes[f"repos/{policy['repository']}/branches/{branch}"] = {
                "name": branch.replace("%2F", "/"), "protected": True, "commit": {"sha": COMMIT}}

        return publication.verify_publisher_environment(FakeApi(routes), policy["repository"], policy, "nuget")

    def test_exact_active_lines_pass(self):
        # Arrange
        grants = [{"type": "branch", "name": "main"}, {"type": "branch", "name": "maintenance/10.x"}]

        # Act
        result = self.check(grants)

        # Assert
        self.assertIsNone(result)

    def test_wildcard_grant_fails(self):
        # Arrange
        grants = [{"type": "branch", "name": "main"}, {"type": "branch", "name": "maintenance/*"}]

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            self.check(grants)

        # Assert
        self.assertIn("exact branch names", str(raised.exception))

    def test_missing_line_grant_fails(self):
        # Arrange
        grants = [{"type": "branch", "name": "main"}]

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            self.check(grants)

        # Assert
        self.assertIn("other branches than the active lines", str(raised.exception))

    def test_environment_without_reviewers_fails(self):
        # Arrange
        grants = [{"type": "branch", "name": "main"}, {"type": "branch", "name": "maintenance/10.x"}]

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            self.check(grants, reviewers=False)

        # Assert
        self.assertIn("no required reviewers", str(raised.exception))


class StageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.manifest = candidate_directory(self.root)
        self.release = {"id": 7, "tag_name": "v1.2.3", "name": "v1.2.3", "draft": True, "prerelease": False,
                        "body": (self.root / publication.NOTES).read_text(encoding="utf-8")}
        self.uploads = []
        self.original = publication.run
        publication.run = lambda command, **kwargs: self.uploads.append(command[3]) or ""

    def tearDown(self):
        publication.run = self.original
        self.directory.cleanup()

    def api(self, assets):
        return FakeApi({f"repos/{REPOSITORY}/releases": [self.release],
                        f"repos/{REPOSITORY}/releases/7/assets": assets})

    def test_only_missing_assets_are_uploaded(self):
        # Arrange
        name = "Doka.Core.1.2.3.nupkg"
        assets = [{"id": 1, "name": name, "state": "uploaded", "size": (self.root / name).stat().st_size,
                   "digest": f"sha256:{packages.sha256(self.root / name)}"}]

        # Act
        result = publication.stage(self.api(assets), REPOSITORY, self.root)

        # Assert
        self.assertNotIn(name, result["uploaded"])
        self.assertIn(publication.CANDIDATE, result["uploaded"])

    def test_asset_with_other_digest_fails(self):
        # Arrange
        name = "Doka.Core.1.2.3.nupkg"
        assets = [{"id": 1, "name": name, "state": "uploaded", "size": (self.root / name).stat().st_size,
                   "digest": "sha256:" + "0" * 64}]

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            publication.stage(self.api(assets), REPOSITORY, self.root)

        # Assert
        self.assertIn("differs from the candidate", str(raised.exception))

    def test_unexpected_draft_asset_fails(self):
        # Arrange
        assets = [{"id": 1, "name": "other.zip", "state": "uploaded", "size": 1, "digest": None}]

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            publication.stage(self.api(assets), REPOSITORY, self.root)

        # Assert
        self.assertIn("unexpected assets: other.zip", str(raised.exception))

    def test_draft_with_other_notes_fails(self):
        # Arrange
        self.release["body"] = "other"

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            publication.stage(self.api([]), REPOSITORY, self.root)

        # Assert
        self.assertIn("another title, notes or prerelease flag", str(raised.exception))


class StarterAssetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        candidate_directory(self.root)
        self.release = {"id": 7, "tag_name": "v1.2.3", "name": "v1.2.3", "draft": True, "prerelease": False,
                        "body": (self.root / publication.NOTES).read_text(encoding="utf-8")}
        self.original = publication.run
        self.uploads = []
        publication.run = lambda command, **kwargs: self.uploads.append(Path(command[4]).name) or ""

    def tearDown(self):
        publication.run = self.original
        self.directory.cleanup()

    def test_starter_asset_is_deleted_and_uploaded_again(self):
        # Arrange
        name = "Doka.Core.1.2.3.nupkg"
        deleted = []

        class Api(FakeApi):
            def request(inner, route, method="GET", payload=None):
                if method == "DELETE":
                    deleted.append(route)
                    return None

                return FakeApi.request(inner, route, method, payload)

        api = Api({f"repos/{REPOSITORY}/releases": [self.release],
                   f"repos/{REPOSITORY}/releases/7/assets": [{"id": 9, "name": name, "state": "starter", "size": 0,
                                                              "digest": None}]})

        # Act
        publication.stage(api, REPOSITORY, self.root)

        # Assert
        self.assertEqual([f"repos/{REPOSITORY}/releases/assets/9"], deleted)
        self.assertIn(name, self.uploads)

    def test_two_assets_with_one_id_fail(self):
        # Arrange
        assets = [{"id": 9, "name": "a.json", "state": "uploaded", "size": 1, "digest": None},
                  {"id": 9, "name": "b.json", "state": "uploaded", "size": 1, "digest": None}]
        api = FakeApi({f"repos/{REPOSITORY}/releases": [self.release], f"repos/{REPOSITORY}/releases/7/assets": assets})

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            publication.stage(api, REPOSITORY, self.root)

        # Assert
        self.assertIn("no unique ID", str(raised.exception))


class AttestationExistsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.subject = Path(self.directory.name) / "candidate.json"
        self.subject.write_text("{}", encoding="ascii")
        self.route = f"repos/{REPOSITORY}/attestations/sha256:{packages.sha256(self.subject)}"

    def tearDown(self):
        self.directory.cleanup()

    def test_repository_without_attestations_means_none(self):
        # Arrange
        api = FakeApi({})

        # Act
        found = attestation.exists(api, REPOSITORY, self.subject, attestation.RELEASE_ADMISSION)

        # Assert
        self.assertFalse(found)

    def test_empty_listing_means_none(self):
        # Arrange
        api = FakeApi({self.route: {"attestations": []}})

        # Act
        found = attestation.exists(api, REPOSITORY, self.subject, attestation.RELEASE_ADMISSION)

        # Assert
        self.assertFalse(found)

    def test_failed_read_is_never_absence(self):
        # Arrange
        api = FakeApi({self.route: GitHubError("x", code="github_http_error", http_status=502)})

        # Act
        with self.assertRaises(GitHubError) as raised:
            attestation.exists(api, REPOSITORY, self.subject, attestation.RELEASE_ADMISSION)

        # Assert
        self.assertEqual(502, raised.exception.http_status)

    def test_listed_attestation_exists(self):
        # Arrange
        api = FakeApi({self.route: {"attestations": [{"bundle": {}}]}})

        # Act
        found = attestation.exists(api, REPOSITORY, self.subject, attestation.RELEASE_ADMISSION)

        # Assert
        self.assertTrue(found)


    def test_listing_is_filtered_by_predicate_type(self):
        # Arrange
        api = RecordingApi({"attestations": []})

        # Act
        attestation.exists(api, REPOSITORY, self.subject, attestation.RELEASE_ADMISSION)

        # Assert
        self.assertIn("&predicate_type=https%3A%2F%2Fgithub.com%2Fdoka-labs%2Fworkflows%2Fblob%2Fmain%2Fdocs%2F"
                      "predicates%2Frelease-admission-v1.md", api.routes[0])


class RecordingApi:
    def __init__(self, answer):
        self.answer = answer
        self.routes = []

    def request(self, route, method="GET", payload=None):
        self.routes.append(route)
        return self.answer


class FirstAdmissionTests(unittest.TestCase):
    def test_provenance_of_the_manifest_is_not_an_earlier_admission(self):
        # Arrange
        directory = Path(tempfile.mkdtemp())
        (directory / publication.CANDIDATE).write_text("{}", encoding="ascii")
        route = f"repos/{REPOSITORY}/attestations/sha256:{packages.sha256(directory / publication.CANDIDATE)}"
        types = {attestation.SLSA_PROVENANCE}

        class TypedApi:
            def request(self, value, method="GET", payload=None):
                requested = unquote(value.split("predicate_type=", 1)[1])
                if value.startswith(route) and requested in types:
                    return {"attestations": [{"bundle": {}}]}

                raise GitHubError("missing", code="github_http_error", http_status=404)

        # Act
        found = attestation.exists(TypedApi(), REPOSITORY, directory / publication.CANDIDATE,
                                   attestation.RELEASE_ADMISSION)

        # Assert
        self.assertFalse(found)


class CompleteDraftTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {"packages": [{"id": "Doka.Core", "nupkg": "Doka.Core.1.2.3.nupkg", "nupkgSha256": "a" * 64,
                                       "snupkg": "Doka.Core.1.2.3.snupkg", "snupkgSha256": "b" * 64}],
                         "sboms": {"Doka.Core.spdx.json": {"sha256": "c" * 64, "package": "Doka.Core.1.2.3.nupkg",
                                                           "predicateType": "https://spdx.dev/Document/v2.2"}},
                         "notesSha256": "d" * 64, "probesSha256": "e" * 64}
        self.data = json.dumps(self.manifest).encode()
        expected = publication.expected_assets(self.manifest)
        self.assets = {name: {"id": index, "name": name, "state": "uploaded", "digest": f"sha256:{digest}"}
                       for index, (name, digest) in enumerate(expected.items())}
        self.assets[publication.CANDIDATE] = {"id": 99, "name": publication.CANDIDATE, "state": "uploaded",
                                              "size": len(self.data)}
        self.attested = {attestation.SLSA_PROVENANCE, "https://spdx.dev/Document/v2.2"}

    def check(self):
        original = (publication.release_assets, publication.asset_bytes, attestation.exists_for_digest)
        publication.release_assets = lambda api, repository, release: self.assets
        publication.asset_bytes = lambda repository, asset: self.data
        attestation.exists_for_digest = lambda api, repository, digest, kind: kind in self.attested
        try:
            publication.require_complete_draft(None, REPOSITORY, {"tag_name": "v1.2.3"})
        finally:
            publication.release_assets, publication.asset_bytes, attestation.exists_for_digest = original

    def test_complete_draft_resumes(self):
        # Arrange
        self.attested.add(attestation.RELEASE_ADMISSION)

        # Act
        result = self.check()

        # Assert
        self.assertIsNone(result)

    def test_missing_asset_stops(self):
        # Arrange
        del self.assets["Doka.Core.1.2.3.snupkg"]

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            self.check()

        # Assert
        self.assertIn("Doka.Core.1.2.3.snupkg", str(raised.exception))

    def test_unfinished_asset_stops(self):
        # Arrange
        self.assets["Doka.Core.spdx.json"]["state"] = "starter"

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            self.check()

        # Assert
        self.assertIn("Doka.Core.spdx.json", str(raised.exception))

    def test_missing_sbom_attestation_stops(self):
        # Arrange
        self.attested.discard("https://spdx.dev/Document/v2.2")

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            self.check()

        # Assert
        self.assertIn("SBOM attestation of Doka.Core.1.2.3.nupkg", str(raised.exception))


class SbomHandoffTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        (self.directory / "Doka.Core.1.2.3.nupkg").write_bytes(b"package")
        (self.directory / "Doka.Core.spdx.json").write_text('{"spdxVersion": "SPDX-2.2"}\n', encoding="utf-8")
        self.manifest = {"packages": [{"id": "Doka.Core", "nupkg": "Doka.Core.1.2.3.nupkg",
                                       "nupkgSha256": packages.sha256(self.directory / "Doka.Core.1.2.3.nupkg")}],
                         "sboms": {"Doka.Core.spdx.json": {
                             "sha256": packages.sha256(self.directory / "Doka.Core.spdx.json"),
                             "package": "Doka.Core.1.2.3.nupkg"}}}
        self.handoff = self.directory / "handoff" / "sboms.json"
        publication.write_sbom_handoff(self.manifest, self.directory, self.handoff)
        self.target = self.directory / "job"
        self.target.mkdir()
        (self.target / "Doka.Core.1.2.3.nupkg").write_bytes(b"package")

    def test_extracted_sbom_has_the_candidate_bytes(self):
        # Arrange
        expected = (self.directory / "Doka.Core.spdx.json").read_bytes()

        # Act
        path = publication.extract_sbom(self.handoff, "Doka.Core", self.target)

        # Assert
        self.assertEqual(expected, path.read_bytes())

    def test_other_package_bytes_fail(self):
        # Arrange
        (self.target / "Doka.Core.1.2.3.nupkg").write_bytes(b"other")

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            publication.extract_sbom(self.handoff, "Doka.Core", self.target)

        # Assert
        self.assertIn("differs from the candidate", str(raised.exception))


class PendingPackagesTests(unittest.TestCase):
    def run_pending(self, states):
        from tools.release_publication import __main__ as cli

        directory = Path(tempfile.mkdtemp())
        manifest = {"packages": [{"id": name, "nupkg": f"{name}.1.0.0.nupkg", "snupkg": f"{name}.1.0.0.snupkg"}
                                 for name in ("Doka.Core", "Doka.Ext")]}
        original = (publication.verify_candidate, nuget.availability)
        publication.verify_candidate = lambda repository, path: manifest
        nuget.availability = lambda path, rows: [{"id": name, "state": state} for name, state in states.items()]
        try:
            with unittest.mock.patch.dict("os.environ", {"GITHUB_REPOSITORY": REPOSITORY}):
                cli.main(["pending-packages", "--candidate", str(directory), "--output", str(directory / "p.txt"),
                          "--symbols-output", str(directory / "s.txt")])
        finally:
            publication.verify_candidate, nuget.availability = original

        return ((directory / "p.txt").read_text(encoding="ascii").split(),
                (directory / "s.txt").read_text(encoding="ascii").split())

    def test_symbols_of_pushed_primaries_are_pushed_again(self):
        # Arrange
        states = {"Doka.Core": "present", "Doka.Ext": "present"}

        # Act
        primaries, symbols = self.run_pending(states)

        # Assert
        self.assertEqual([], primaries)
        self.assertEqual(["Doka.Core.1.0.0.snupkg", "Doka.Ext.1.0.0.snupkg"], symbols)

    def test_only_absent_primaries_are_pending(self):
        # Arrange
        states = {"Doka.Core": "present", "Doka.Ext": "absent"}

        # Act
        primaries, symbols = self.run_pending(states)

        # Assert
        self.assertEqual(["Doka.Ext.1.0.0.nupkg"], primaries)
        self.assertEqual(2, len(symbols))


class UnusedVersionTests(unittest.TestCase):
    def test_present_version_is_reported(self):
        # Arrange
        url = "https://api.nuget.org/v3-flatcontainer/doka.core/1.2.3/doka.core.1.2.3.nupkg"
        fetcher = FakeFetcher({url: (200, b"x")})

        # Act
        states = nuget.unused(["Doka.Core", "Doka.Other"], "1.2.3", fetcher=fetcher)

        # Assert
        self.assertEqual({"Doka.Core": "present", "Doka.Other": "absent"}, states)

    def test_unclear_answer_fails(self):
        # Arrange
        url = "https://api.nuget.org/v3-flatcontainer/doka.core/1.2.3/doka.core.1.2.3.nupkg"
        fetcher = FakeFetcher({url: (503, b"")})

        # Act
        with self.assertRaises(nuget.NuGetError) as raised:
            nuget.unused(["Doka.Core"], "1.2.3", fetcher=fetcher)

        # Assert
        self.assertIn("HTTP 503", str(raised.exception))


class TagAndAdmissionTests(unittest.TestCase):
    def test_tags_on_the_commit_are_found_annotated_or_lightweight(self):
        # Arrange
        listing = (f"{'e' * 40}\trefs/tags/v1.0.0\n{COMMIT}\trefs/tags/v1.0.0^{{}}\n"
                   f"{COMMIT}\trefs/tags/light\n{'f' * 40}\trefs/tags/v0.9.0\n")
        original = publication.run
        publication.run = lambda command, **kwargs: listing

        # Act
        try:
            tags = publication.tags_on_commit(Path("."), COMMIT)
        finally:
            publication.run = original

        # Assert
        self.assertEqual(["light", "v1.0.0"], tags)

    def test_earlier_admission_of_same_candidate_passes(self):
        # Arrange
        manifest = {"tagObject": "t", "commit": COMMIT, "tree": "c", "version": "1.2.3"}

        # Act
        result = publication.require_same_admissions([{"predicate": dict(manifest)}], manifest)

        # Assert
        self.assertIsNone(result)

    def test_earlier_admission_of_other_tag_object_fails(self):
        # Arrange
        manifest = {"tagObject": "t", "commit": COMMIT, "tree": "c", "version": "1.2.3"}

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            publication.require_same_admissions([{"predicate": {**manifest, "tagObject": "other"}}], manifest)

        # Assert
        self.assertIn("another tag object or candidate", str(raised.exception))


class PreTagHelperTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / ".github").mkdir()
        (self.root / publication.ALLOWED_SIGNERS).write_text("dev@example ssh-ed25519 AAAAKEY\n", encoding="utf-8")

    def tearDown(self):
        self.directory.cleanup()

    def test_registered_public_key_file_passes(self):
        # Arrange
        (self.root / "id.pub").write_text("ssh-ed25519 AAAAKEY dev@example\n", encoding="utf-8")

        # Act
        registered = publication.signing_key_registered(self.root, str(self.root / "id.pub"))

        # Assert
        self.assertTrue(registered)

    def test_private_key_path_uses_its_public_key(self):
        # Arrange
        (self.root / "id").write_text("private", encoding="utf-8")
        (self.root / "id.pub").write_text("ssh-ed25519 AAAAKEY dev@example\n", encoding="utf-8")

        # Act
        registered = publication.signing_key_registered(self.root, str(self.root / "id"))

        # Assert
        self.assertTrue(registered)

    def test_unregistered_key_fails(self):
        # Arrange
        key = "key::ssh-ed25519 OTHERKEY"

        # Act
        registered = publication.signing_key_registered(self.root, key)

        # Assert
        self.assertFalse(registered)

    def test_origin_url_forms_name_the_repository(self):
        # Arrange
        urls = ["git@github.com:doka-labs/Example.git", "https://github.com/doka-labs/Example"]
        original = publication.run
        results = []

        # Act
        try:
            for url in urls:
                publication.run = lambda command, url=url, **kwargs: url + "\n"
                results.append(publication.repository_of(Path(".")))
        finally:
            publication.run = original

        # Assert
        self.assertEqual(["doka-labs/Example", "doka-labs/Example"], results)


if __name__ == "__main__":
    unittest.main()
