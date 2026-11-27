import base64
import hashlib
import json
import os
import shutil
import subprocess
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

    def test_link_references_after_the_oldest_version_stay_out_of_its_notes(self):
        # Arrange
        text = ("## [Unreleased]\n\n## [1.2.3] - 2026-11-22\n\n- First.\n\n"
                "[Unreleased]: https://github.com/doka-labs/Example/compare/v1.2.3...HEAD\n"
                "[1.2.3]: https://github.com/doka-labs/Example/releases/tag/v1.2.3\n")

        # Act
        notes = self.notes(text)

        # Assert
        self.assertEqual("# 1.2.3\n\n- First.\n", notes)

    def test_section_of_only_link_references_has_no_notes(self):
        # Arrange
        text = "## [1.2.3] - 2026-11-22\n\n[1.2.3]: https://github.com/doka-labs/Example/releases/tag/v1.2.3\n"

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            self.notes(text)

        # Assert
        self.assertIn("no notes", str(raised.exception))

    def test_parenthesized_date_heading_fails(self):
        # Arrange
        text = "## 1.2.3 (2026-11-22)\n\nNotes.\n"

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            self.notes(text)

        # Assert
        self.assertIn("exactly one dated section for 1.2.3", str(raised.exception))

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
        self.headers = {}

    def __call__(self, url, headers, timeout):
        self.headers[url] = headers
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
        result = nuget.readback(self.root, self.inventory, [probe], self.root / "work", self.sdk(), fetcher=fetcher,
                                verifier=lambda path, timeout: None, clock=lambda: now[0],
                                sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))

        # Assert
        self.assertEqual(["Doka.Core"], [item["id"] for item in result["packages"]])
        self.assertEqual([probe["url"]], result["symbols"])

    def probe(self, content=b"pdb"):
        return {"url": "https://symbols.nuget.org/download/symbols/x.pdb/k/x.pdb", "checksum": "SHA256:00",
                "pdb": "lib/net10.0/Doka.Core.pdb", "package": self.inventory[0]["nupkg"],
                "sha256": hashlib.sha256(content).hexdigest()}

    def test_symbols_the_server_serves_are_not_pending(self):
        # Arrange
        probe = self.probe()
        fetcher = FakeFetcher({probe["url"]: (200, b"pdb")})

        # Act
        pending = nuget.pending_symbols(self.inventory, [probe], fetcher=fetcher)

        # Assert
        self.assertEqual([], pending)
        self.assertEqual({"SymbolChecksum": "SHA256:00"}, fetcher.headers[probe["url"]])

    def test_symbols_the_server_does_not_serve_yet_are_pending(self):
        # Arrange
        probe = self.probe()
        fetcher = FakeFetcher({probe["url"]: (404, b"")})

        # Act
        pending = nuget.pending_symbols(self.inventory, [probe], fetcher=fetcher)

        # Assert
        self.assertEqual([self.inventory[0]["snupkg"]], [package["snupkg"] for package in pending])

    def test_symbols_with_other_bytes_on_the_server_fail(self):
        # Arrange
        probe = self.probe()
        fetcher = FakeFetcher({probe["url"]: (200, b"other")})

        # Act
        with self.assertRaises(nuget.NuGetError) as raised:
            nuget.pending_symbols(self.inventory, [probe], fetcher=fetcher)

        # Assert
        self.assertIn("serves another", str(raised.exception))

    def test_unclear_symbol_answers_fail_instead_of_pending(self):
        for status in (0, 403, 408, 429, 503):
            with self.subTest(status=status):
                # Arrange
                probe = self.probe()
                fetcher = FakeFetcher({probe["url"]: (status, b"")})

                # Act
                with self.assertRaises(nuget.NuGetError) as raised:
                    nuget.pending_symbols(self.inventory, [probe], fetcher=fetcher)

                # Assert
                self.assertNotIsInstance(raised.exception, nuget.PendingError)
                self.assertIn(f"Cannot establish the state of {probe['pdb']}", str(raised.exception))

    def test_other_bytes_fail_in_either_order_with_an_absent_pdb(self):
        for absent_first in (True, False):
            with self.subTest(absent_first=absent_first):
                # Arrange
                absent = {**self.probe(), "url": "https://symbols.nuget.org/download/symbols/a.pdb/k/a.pdb",
                          "pdb": "lib/net10.0/a.pdb"}
                other = {**self.probe(), "url": "https://symbols.nuget.org/download/symbols/b.pdb/k/b.pdb",
                         "pdb": "lib/net10.0/b.pdb"}
                probes = [absent, other] if absent_first else [other, absent]
                fetcher = FakeFetcher({absent["url"]: (404, b""), other["url"]: (200, b"other")})

                # Act
                with self.assertRaises(nuget.NuGetError) as raised:
                    nuget.pending_symbols(self.inventory, probes, fetcher=fetcher)

                # Assert
                self.assertIn("serves another lib/net10.0/b.pdb", str(raised.exception))

    def test_package_with_one_absent_pdb_is_pending(self):
        # Arrange
        served = {**self.probe(), "url": "https://symbols.nuget.org/download/symbols/a.pdb/k/a.pdb"}
        absent = {**self.probe(), "url": "https://symbols.nuget.org/download/symbols/b.pdb/k/b.pdb"}
        fetcher = FakeFetcher({served["url"]: (200, b"pdb"), absent["url"]: (404, b"")})

        # Act
        pending = nuget.pending_symbols(self.inventory, [served, absent], fetcher=fetcher)

        # Assert
        self.assertEqual([self.inventory[0]["snupkg"]], [package["snupkg"] for package in pending])

    def test_package_without_a_symbol_probe_fails(self):
        # Arrange
        fetcher = FakeFetcher({})

        # Act
        with self.assertRaises(nuget.NuGetError) as raised:
            nuget.pending_symbols(self.inventory, [], fetcher=fetcher)

        # Assert
        self.assertIn("has no symbol probe", str(raised.exception))

    def sdk(self):
        path = self.root / "product-global.json"
        path.write_text('{"sdk": {"version": "10.0.404"}}', encoding="ascii")
        return path

    def test_readback_uses_the_product_sdk(self):
        # Arrange
        fetcher = FakeFetcher({self.url: [(200, self.signed())]})
        seen = []

        # Act
        nuget.readback(self.root, self.inventory, [], self.root / "work", self.sdk(), fetcher=fetcher,
                       verifier=lambda path, timeout: seen.append((path.parent / "global.json").read_text()),
                       clock=lambda: 0.0, sleep=lambda seconds: None)

        # Assert
        self.assertEqual(['{"sdk": {"version": "10.0.404"}}'], seen)

    def test_duplicate_probes_of_one_pdb_complete(self):
        # Arrange
        probe = {"url": "https://symbols.nuget.org/download/symbols/x.pdb/k/x.pdb", "checksum": "SHA256:00",
                 "pdb": "lib/net10.0/Doka.Core.pdb", "sha256": __import__("hashlib").sha256(b"pdb").hexdigest()}
        fetcher = FakeFetcher({self.url: [(200, self.signed())], probe["url"]: [(200, b"pdb")]})

        # Act
        result = nuget.readback(self.root, self.inventory, [probe, dict(probe)], self.root / "work", self.sdk(),
                                fetcher=fetcher, verifier=lambda path, timeout: None, clock=lambda: 0.0,
                                sleep=lambda seconds: None, timeout=30)

        # Assert
        self.assertEqual([probe["url"]], result["symbols"])

    def test_readback_deadline_is_pending_not_failure(self):
        # Arrange
        fetcher = FakeFetcher({})
        now = [0.0]

        # Act
        with self.assertRaises(nuget.PendingError) as raised:
            nuget.readback(self.root, self.inventory, [], self.root / "work", self.sdk(), fetcher=fetcher,
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

    def fake(self, *, qualification=None, subjects=None, sbom=SBOM, origin_attempt=1):
        package = self.manifest["packages"][0]
        subjects = subjects if subjects is not None else {package["nupkg"]: package["nupkgSha256"],
                                                          package["snupkg"]: package["snupkgSha256"]}
        qualification = qualification if qualification is not None else [
            {"predicate": {"commit": COMMIT, "version": "1.2.3", "snapshot": {"qualificationOnly": False}},
             "runId": 1, "attempt": 1, "subjects": {}}]

        def verify(subject, repository, predicate_type, **options):
            self.calls.append((predicate_type, options))
            if predicate_type == attestation.SLSA_PROVENANCE:
                return [{"predicate": {}, "runId": 1, "attempt": origin_attempt, "subjects": subjects}]

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
        fake = self.fake(qualification=[{"predicate": {"commit": "b" * 40, "version": "1.2.3"}, "runId": 1,
                                         "attempt": 1, "subjects": {}}])

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            publication.verify_candidate(REPOSITORY, self.root, verify=fake)

        # Assert
        self.assertIn("No release qualification", str(raised.exception))

    def test_candidate_uses_its_own_run_and_allows_earlier_attempt(self):
        # Arrange
        qualification = [
            {"runId": run, "attempt": attempt, "predicate": {"commit": COMMIT, "version": "1.2.3",
                                                              "snapshot": {"qualificationOnly": only}}}
            for run, attempt, only in [(1, 1, False), (2, 1, True), (1, 3, True)]]
        fake = self.fake(qualification=qualification, origin_attempt=2)

        # Act
        manifest = publication.verify_candidate(REPOSITORY, self.root, verify=fake)

        # Assert
        self.assertIs(manifest["qualification"]["snapshot"]["qualificationOnly"], False)

    def validated(self, digest=None):
        package = self.manifest["packages"][0]
        return [{"runId": 1, "attempt": 1, "subjects": {}, "predicate": {
            "commit": COMMIT, "version": "1.2.3", "snapshot": {"qualificationOnly": False},
            "packageValidation": [{"package": package["id"], "project": "src/Core/Core.csproj",
                                   "nupkg": package["nupkg"], "sha256": digest or package["nupkgSha256"]}]}}]

    def test_candidate_whose_qualification_validated_its_archives_passes(self):
        # Arrange
        fake = self.fake(qualification=self.validated())

        # Act
        manifest = publication.verify_candidate(REPOSITORY, self.root, verify=fake)

        # Assert
        self.assertEqual(1, len(manifest["qualification"]["packageValidation"]))

    def test_candidate_whose_qualification_validated_other_archives_fails(self):
        # Arrange
        fake = self.fake(qualification=self.validated("0" * 64))

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            publication.verify_candidate(REPOSITORY, self.root, verify=fake)

        # Assert
        self.assertIn("validated package archives differ", str(raised.exception))

    def test_new_candidate_needs_validated_archives(self):
        # Arrange
        qualification = {"packageValidation": [{"package": "Doka.Core", "project": "src/Core/Core.csproj"}]}

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            publication.require_validated_archives(qualification, self.manifest["packages"], required=True)

        # Assert
        self.assertIn("does not record the validated package archives", str(raised.exception))

    def test_earlier_candidate_without_validated_archives_stays_publishable(self):
        # Arrange
        qualification = {"packageValidation": [{"package": "Doka.Core", "project": "src/Core/Core.csproj"}]}

        # Act
        result = publication.require_validated_archives(qualification, self.manifest["packages"], required=False)

        # Assert
        self.assertIsNone(result)

    def test_qualification_only_candidate_is_rejected_during_verification(self):
        # Arrange
        qualification = [{"runId": 1, "attempt": 1, "predicate": {"commit": COMMIT, "version": "1.2.3",
                                                                   "snapshot": {"qualificationOnly": True}}}]
        fake = self.fake(qualification=qualification)

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            publication.verify_candidate(REPOSITORY, self.root, verify=fake)

        # Assert
        self.assertIn("qualification-only", str(raised.exception))

    def test_conflicting_symbol_probes_fail_during_candidate_verification(self):
        # Arrange
        probes = self.manifest["symbols"]
        probes.append({**probes[0], "sha256": "different"})
        path = self.root / publication.PROBES
        path.write_text(json.dumps({"probes": probes}), encoding="ascii")
        self.manifest["probesSha256"] = packages.sha256(path)
        (self.root / publication.CANDIDATE).write_text(json.dumps(self.manifest), encoding="ascii")
        fake = self.fake()

        # Act
        with self.assertRaises(nuget.NuGetError) as raised:
            publication.verify_candidate(REPOSITORY, self.root, verify=fake)

        # Assert
        self.assertIn("expect different bytes", str(raised.exception))
        self.assertEqual([], self.calls)

    def test_duplicate_manifest_key_fails(self):
        # Arrange
        text = (self.root / publication.CANDIDATE).read_text(encoding="ascii")
        repeated = text.replace('{"schemaVersion": 1,', '{"schemaVersion": 1, "schemaVersion": 1,', 1)
        (self.root / publication.CANDIDATE).write_text(repeated, encoding="ascii")

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            publication.verify_candidate(REPOSITORY, self.root, verify=self.fake())

        # Assert
        self.assertIn("repeats the key schemaVersion", str(raised.exception))


class OwnTokenRequestTests(unittest.TestCase):
    def test_release_tools_request_no_oidc_token_of_their_own(self):
        # Arrange
        # WHY: Only NuGet/login and actions/attest request the job's OIDC token, as in MySql, SafeMigrations and
        # NestedSet; the tools' own request returned HTTP 400 in Tabular's first publication (run 45886213179).
        sources = sorted(Path(publication.__file__).parent.glob("*.py"))

        # Act
        readers = [path.name for path in sources if "ACTIONS_ID_TOKEN_REQUEST" in path.read_text(encoding="utf-8")]

        # Assert
        self.assertEqual([], readers)


class AdmissionSignerTests(unittest.TestCase):
    def test_admission_signer_is_the_release_workflow_of_the_repository(self):
        # Arrange
        policy = {"repository": "doka-labs/Doka.Tabular",
                  "release": {"workflowPath": ".github/workflows/release-candidate.yml"}}

        # Act
        signer = attestation.release_signer(policy)

        # Assert
        self.assertEqual("doka-labs/Doka.Tabular/.github/workflows/release-candidate.yml", signer)

    def test_admission_without_a_named_signer_is_not_verified(self):
        # Arrange
        calls = []

        # Act
        with self.assertRaises(GitHubError) as raised:
            attestation.verify(Path("candidate.json"), REPOSITORY, attestation.RELEASE_ADMISSION,
                               run=lambda *args, **kwargs: calls.append(args))

        # Assert
        self.assertIn("No signer is known", str(raised.exception))
        self.assertEqual([], calls)


class EnvironmentTests(unittest.TestCase):
    def check(self, grants, *, reviewers=True):
        policy = json.loads((Path(__file__).resolve().parents[3] / "examples" / "release-policy.json").read_text())
        base = f"repos/{policy['repository']}/environments/nuget"
        rule = {"type": "required_reviewers", "reviewers": [{"id": 1}] if reviewers else []}
        routes = {base: {"protection_rules": [rule],
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

    def test_asset_with_other_digest_of_a_complete_candidate_fails(self):
        # Arrange
        name = "Doka.Core.1.2.3.nupkg"
        assets = [{"id": 1, "name": name, "state": "uploaded", "size": (self.root / name).stat().st_size,
                   "digest": "sha256:" + "0" * 64},
                  {"id": 2, "name": publication.CANDIDATE, "state": "uploaded",
                   "size": (self.root / publication.CANDIDATE).stat().st_size, "digest": None}]

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            publication.stage(self.api(assets), REPOSITORY, self.root)

        # Assert
        self.assertIn("differs from the candidate", str(raised.exception))

    def test_asset_of_an_unfinished_attempt_is_replaced(self):
        # Arrange
        name = "Doka.Core.spdx.json"
        assets = [{"id": 1, "name": name, "state": "uploaded", "size": 3, "digest": "sha256:" + "0" * 64}]
        api = RecordingFakeApi({f"repos/{REPOSITORY}/releases": [self.release],
                                f"repos/{REPOSITORY}/releases/7/assets": assets})

        # Act
        result = publication.stage(api, REPOSITORY, self.root)

        # Assert
        self.assertIn(("DELETE", f"repos/{REPOSITORY}/releases/assets/1"), api.calls)
        self.assertIn(name, result["uploaded"])

    def test_manifest_is_uploaded_last(self):
        # Arrange
        assets = []

        # Act
        result = publication.stage(self.api(assets), REPOSITORY, self.root)

        # Assert
        self.assertEqual(publication.CANDIDATE, result["uploaded"][-1])

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


class RecordingFakeApi(FakeApi):
    def __init__(self, routes):
        super().__init__(routes)
        self.calls = []

    def request(self, route, method="GET", payload=None):
        self.calls.append((method, route))
        if method == "DELETE":
            return None

        return super().request(route, method, payload)


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
                                              "digest": f"sha256:{__import__('hashlib').sha256(self.data).hexdigest()}"}
        self.verified = []
        self.failure = None

    def check(self):
        def verify_candidate(repository, directory, verify=None):
            self.verified.append(sorted(path.name for path in directory.iterdir()))
            if self.failure is not None:
                raise self.failure

            return self.manifest

        original = (publication.release_assets, publication.asset_bytes, publication.verify_candidate)
        publication.release_assets = lambda api, repository, release: self.assets
        publication.asset_bytes = lambda repository, asset: self.data
        publication.verify_candidate = verify_candidate
        try:
            publication.require_complete_draft(None, REPOSITORY, {"tag_name": "v1.2.3"})
        finally:
            publication.release_assets, publication.asset_bytes, publication.verify_candidate = original

    def test_complete_draft_is_verified_like_publish(self):
        # Arrange
        expected = sorted(self.assets)

        # Act
        self.check()

        # Assert
        self.assertEqual([expected], self.verified)

    def test_missing_asset_stops_before_verification(self):
        # Arrange
        del self.assets["Doka.Core.1.2.3.snupkg"]

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            self.check()

        # Assert
        self.assertIn("Doka.Core.1.2.3.snupkg", str(raised.exception))
        self.assertEqual([], self.verified)

    def test_unfinished_asset_stops(self):
        # Arrange
        self.assets["Doka.Core.spdx.json"]["state"] = "starter"

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            self.check()

        # Assert
        self.assertIn("Doka.Core.spdx.json", str(raised.exception))

    def test_sbom_attestation_with_other_content_stops(self):
        # Arrange
        self.failure = publication.PublicationError("No SBOM attestation of Doka.Core.1.2.3.nupkg equals x.")

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            self.check()

        # Assert
        self.assertIn("cannot be published (No SBOM attestation", str(raised.exception))


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
    def run_pending(self, states, served):
        from tools.release_publication import __main__ as cli

        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        manifest = {"packages": [{"id": name, "nupkg": f"{name}.1.0.0.nupkg", "snupkg": f"{name}.1.0.0.snupkg"}
                                 for name in ("Doka.Core", "Doka.Ext")], "symbols": []}
        output = directory / "github-output.txt"
        with unittest.mock.patch.object(publication, "verify_candidate", lambda repository, path: manifest), \
                unittest.mock.patch.object(nuget, "availability", lambda path, rows: [
                    {"id": name, "state": state} for name, state in states.items()]), \
                unittest.mock.patch.object(nuget, "pending_symbols", lambda rows, probes: [
                    row for row in rows if row["id"] not in served]), \
                unittest.mock.patch.dict("os.environ", {"GITHUB_REPOSITORY": REPOSITORY,
                                                        "GITHUB_OUTPUT": str(output)}):
            cli.main(["pending-packages", "--candidate", str(directory), "--output", str(directory / "p.txt"),
                      "--symbols-output", str(directory / "s.txt")])

        return ((directory / "p.txt").read_text(encoding="ascii").split(),
                (directory / "s.txt").read_text(encoding="ascii").split(), output.read_text(encoding="ascii"))

    def test_published_release_has_nothing_to_push(self):
        # Arrange
        states = {"Doka.Core": "matching", "Doka.Ext": "matching"}

        # Act
        primaries, symbols, output = self.run_pending(states, {"Doka.Core", "Doka.Ext"})

        # Assert
        self.assertEqual(([], []), (primaries, symbols))
        self.assertEqual("push=false\n", output)

    def test_symbols_not_served_are_pushed_although_the_primary_is_on_nuget(self):
        # Arrange
        states = {"Doka.Core": "matching", "Doka.Ext": "matching"}

        # Act
        primaries, symbols, output = self.run_pending(states, {"Doka.Core"})

        # Assert
        self.assertEqual(([], ["Doka.Ext.1.0.0.snupkg"]), (primaries, symbols))
        self.assertEqual("push=true\n", output)

    def test_only_absent_primaries_are_pending(self):
        # Arrange
        states = {"Doka.Core": "matching", "Doka.Ext": "absent"}

        # Act
        primaries, symbols, output = self.run_pending(states, {"Doka.Core"})

        # Assert
        self.assertEqual(["Doka.Ext.1.0.0.nupkg"], primaries)
        self.assertEqual(["Doka.Ext.1.0.0.snupkg"], symbols)
        self.assertEqual("push=true\n", output)


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


class PublishCurrentDraftTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.manifest = candidate_directory(self.root)
        self.release = {"id": 7, "tag_name": "v1.2.3", "name": "v1.2.3", "draft": True, "prerelease": False,
                        "body": (self.root / publication.NOTES).read_text(encoding="utf-8")}
        names = {*publication.expected_assets(self.manifest), publication.CANDIDATE}
        self.assets = [{"id": index, "name": name, "state": "uploaded",
                        "digest": f"sha256:{packages.sha256(self.root / name)}"} for index, name in enumerate(names)]

    def tearDown(self):
        self.directory.cleanup()

    def test_recheck_blocks_unsupported_latest_without_writing(self):
        # Arrange
        from tools.release_publication import __main__ as cli

        api = RecordingFakeApi({f"repos/{REPOSITORY}/releases": [self.release],
                                f"repos/{REPOSITORY}/releases/latest": {"tag_name": "v1.0.0", "immutable": False}})
        mock = unittest.mock

        # Act
        with mock.patch.dict(cli.os.environ, {"GITHUB_REPOSITORY": REPOSITORY}), \
                mock.patch.object(cli, "GitHub", return_value=api), \
                mock.patch.object(publication, "authorize", return_value=self.manifest):
            result = cli.main(["recheck", "--root", str(self.root), "--candidate", str(self.root)])

        # Assert
        self.assertEqual(1, result)
        self.assertEqual([("GET", f"repos/{REPOSITORY}/releases?per_page=100&page=1"),
                          ("GET", f"repos/{REPOSITORY}/releases/latest")], api.calls)

    def test_draft_changed_after_verification_is_not_published(self):
        # Arrange
        self.assets[0]["digest"] = "sha256:" + "0" * 64
        api = RecordingFakeApi({f"repos/{REPOSITORY}/releases": [self.release],
                                f"repos/{REPOSITORY}/releases/7/assets": self.assets})

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            publication.publish_release(api, REPOSITORY, self.manifest, self.root, attempts=1, sleep=lambda s: None)

        # Assert
        self.assertIn("changed since its verification", str(raised.exception))
        self.assertNotIn("PATCH", [method for method, route in api.calls])

    def test_unchanged_draft_is_published(self):
        # Arrange
        api = RecordingFakeApi({f"repos/{REPOSITORY}/releases": [self.release],
                                f"repos/{REPOSITORY}/releases/7/assets": self.assets,
                                f"repos/{REPOSITORY}/releases/latest": {"tag_name": "v1.0.0"},
                                f"repos/{REPOSITORY}/releases/7": {}})
        original = publication.latest_flag
        publication.latest_flag = lambda api, repository, version: False
        self.addCleanup(setattr, publication, "latest_flag", original)

        # Act
        with self.assertRaises(publication.PublicationError):
            publication.publish_release(api, REPOSITORY, self.manifest, self.root, attempts=1, sleep=lambda s: None)

        # Assert
        self.assertIn(("PATCH", f"repos/{REPOSITORY}/releases/7"), api.calls)


class StageWithoutDigestTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.manifest = candidate_directory(self.root)
        self.release = {"id": 7, "tag_name": "v1.2.3", "name": "v1.2.3", "draft": True, "prerelease": False,
                        "body": (self.root / publication.NOTES).read_text(encoding="utf-8")}
        sbom = self.root / "Doka.Core.spdx.json"
        self.old = bytes(reversed(sbom.read_bytes()))
        self.assets = [{"id": 1, "name": sbom.name, "state": "uploaded", "size": sbom.stat().st_size,
                        "digest": None}]
        originals = (publication.run, publication.asset_bytes)
        publication.run = lambda command, **kwargs: ""
        publication.asset_bytes = lambda repository, asset: self.old
        self.addCleanup(lambda: (setattr(publication, "run", originals[0]),
                                 setattr(publication, "asset_bytes", originals[1])))

    def tearDown(self):
        self.directory.cleanup()

    def test_same_size_other_bytes_of_an_unfinished_attempt_are_replaced(self):
        # Arrange
        api = RecordingFakeApi({f"repos/{REPOSITORY}/releases": [self.release],
                                f"repos/{REPOSITORY}/releases/7/assets": self.assets})

        # Act
        result = publication.stage(api, REPOSITORY, self.root)

        # Assert
        self.assertIn(("DELETE", f"repos/{REPOSITORY}/releases/assets/1"), api.calls)
        self.assertIn("Doka.Core.spdx.json", result["uploaded"])

    def test_same_size_other_bytes_of_a_complete_candidate_fail(self):
        # Arrange
        candidate = self.root / publication.CANDIDATE
        self.assets.append({"id": 2, "name": candidate.name, "state": "uploaded", "size": candidate.stat().st_size,
                            "digest": f"sha256:{packages.sha256(candidate)}"})
        api = RecordingFakeApi({f"repos/{REPOSITORY}/releases": [self.release],
                                f"repos/{REPOSITORY}/releases/7/assets": self.assets})

        # Act
        with self.assertRaises(publication.PublicationError) as raised:
            publication.stage(api, REPOSITORY, self.root)

        # Assert
        self.assertIn("Doka.Core.spdx.json of the draft differs", str(raised.exception))


class PublishedInventoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.manifest = candidate_directory(self.root)
        names = {*publication.expected_assets(self.manifest), publication.CANDIDATE}
        self.assets = [{"id": index, "name": name, "state": "uploaded",
                        "digest": f"sha256:{packages.sha256(self.root / name)}"} for index, name in enumerate(names)]
        self.release = {"id": 7, "tag_name": "v1.2.3", "name": "v1.2.3", "draft": True, "prerelease": False,
                        "body": (self.root / publication.NOTES).read_text(encoding="utf-8")}
        self.order = []
        originals = (publication.run, publication.latest_flag, publication.release_assets)
        publication.run = lambda command, **kwargs: ""
        publication.latest_flag = lambda api, repository, version: self.order.append("latest") or False
        self.addCleanup(lambda: (setattr(publication, "run", originals[0]),
                                 setattr(publication, "latest_flag", originals[1]),
                                 setattr(publication, "release_assets", originals[2])))

    def tearDown(self):
        self.directory.cleanup()

    def test_extra_asset_after_publication_fails_without_retry(self):
        # Arrange
        releases = [self.release, {**self.release, "draft": False, "immutable": True}]
        inventories = [self.assets, self.assets + [{"id": 50, "name": "extra.zip", "state": "uploaded",
                                                    "digest": "sha256:" + "1" * 64}]]
        original = publication.release_for
        publication.release_for = lambda api, repository, tag: releases.pop(0) if len(releases) > 1 else releases[0]
        self.addCleanup(setattr, publication, "release_for", original)
        publication.release_assets = lambda api, repository, release: (
            self.order.append("assets") or {asset["name"]: asset for asset in inventories.pop(0)})
        api = RecordingFakeApi({f"repos/{REPOSITORY}/releases/7": {}})

        # Act
        with self.assertRaises(publication.ReleaseInventoryError) as raised:
            publication.publish_release(api, REPOSITORY, self.manifest, self.root, attempts=3, sleep=lambda s: None)

        # Assert
        self.assertIn("extra.zip", str(raised.exception))
        self.assertEqual(["latest", "assets", "assets"], self.order)
        self.assertIn(("PATCH", f"repos/{REPOSITORY}/releases/7"), api.calls)


class ProbeConsistencyTests(unittest.TestCase):
    def test_probes_of_one_url_with_other_bytes_fail(self):
        # Arrange
        root = Path(tempfile.mkdtemp())
        sdk = root / "global.json"
        sdk.write_text("{}", encoding="ascii")
        probe = {"url": "https://symbols.nuget.org/x.pdb", "checksum": "SHA256:00", "pdb": "x.pdb", "sha256": "a" * 64}

        # Act
        with self.assertRaises(nuget.NuGetError) as raised:
            nuget.readback(root, [], [probe, {**probe, "sha256": "b" * 64}], root / "work", sdk,
                           fetcher=FakeFetcher({}), verifier=lambda path, timeout: None, clock=lambda: 0.0,
                           sleep=lambda seconds: None, timeout=0)

        # Assert
        self.assertIn("expect different bytes at one URL", str(raised.exception))


class TagReadinessTests(unittest.TestCase):
    def run_check(self, *, tag_taken=False, other_tags=(), taken=False, draft_commit=COMMIT, failure=None,
                  signers="principal namespaces=\"git\" ssh-ed25519 K\n"):
        manifest = json.dumps({"commit": draft_commit, "version": "1.2.3"}).encode()
        checked = []

        def complete(api, repository, release, verify=None):
            checked.append(release["id"])
            if failure is not None:
                raise failure

        patches = {
            "release_notes": lambda root, version: "notes", "run": lambda command, **kwargs: "",
            "observe_authority": lambda api, repository: {"policy": {"release": {"packages": ["Doka.Core"]}},
                                                          "commit": "m" * 40},
            "current_signers": lambda api, repository, commit: signers if commit == "m" * 40 else "",
            "tag_exists": lambda api, repository, tag: tag_taken,
            "tags_on_commit": lambda root, commit: list(other_tags),
            "release_for": lambda api, repository, tag: {"id": 7, "draft": True, "tag_name": tag},
            "release_assets": lambda api, repository, release: {publication.CANDIDATE: {"id": 1, "size": 1}},
            "asset_bytes": lambda repository, asset: manifest, "require_complete_draft": complete}
        environment = {"GITHUB_REPOSITORY": REPOSITORY, "GITHUB_SHA": COMMIT}
        with unittest.mock.patch.multiple(publication, **patches), \
                unittest.mock.patch.object(publication.nuget, "unused",
                                           return_value={"Doka.Core": "listed" if taken else "absent"}):
            result = publication.tag_readiness(None, environment, Path("."), "1.2.3")

        return result, checked

    @staticmethod
    def failed(result):
        return {item["subject"]: item["detail"] for item in result["checks"] if not item["ok"]}

    def test_passing_checks_report_every_check(self):
        # Arrange
        expected = list(publication.TAG_READINESS_CHECKS)

        # Act
        result, checked = self.run_check()

        # Assert
        self.assertEqual(expected, [item["subject"] for item in result["checks"]])
        self.assertEqual((0, "v1.2.3", COMMIT), (result["failures"], result["tag"], result["commit"]))
        self.assertEqual([7], checked)

    def test_independent_failures_are_all_reported(self):
        # Arrange
        expected = {"Tag unused", "Version unused on nuget.org", "Allowed signers listed"}

        # Act
        result, checked = self.run_check(tag_taken=True, taken=True, signers="# no key yet\n")

        # Assert
        self.assertEqual(expected, set(self.failed(result)))
        self.assertIn("Choose the next version.", self.failed(result)["Tag unused"])
        self.assertEqual([7], checked)

    def test_commit_with_another_version_tag_fails(self):
        # Arrange
        other_tags = ("v1.2.2",)

        # Act
        result, _ = self.run_check(other_tags=other_tags)

        # Assert
        self.assertIn("already carries the tags v1.2.2", self.failed(result)["No other tag on the commit"])

    def test_draft_of_another_commit_marks_its_verification_as_not_checked(self):
        # Arrange
        draft_commit = "b" * 40

        # Act
        result, checked = self.run_check(draft_commit=draft_commit)

        # Assert
        failed = self.failed(result)
        self.assertIn(f"built from {draft_commit}, not from {COMMIT}", failed["Draft holds this run's candidate"])
        self.assertTrue(failed["Draft passes publish verification"].startswith("Not checked: needs release"))
        self.assertEqual([], checked)
        self.assertEqual(len(publication.TAG_READINESS_CHECKS), len(result["checks"]))

    def test_draft_failing_verification_fails(self):
        # Arrange
        failure = publication.PublicationError("The draft of v1.2.3 cannot be published (No SBOM attestation).")

        # Act
        result, _ = self.run_check(failure=failure)

        # Assert
        self.assertIn("No SBOM attestation", self.failed(result)["Draft passes publish verification"])


class TagReportTests(unittest.TestCase):
    def report(self, failures):
        from tools.release_publication import __main__ as cli

        checks = [{"subject": name, "ok": True, "detail": ""} for name in publication.TAG_READINESS_CHECKS]
        if failures:
            checks[1] = {"subject": checks[1]["subject"], "ok": False, "detail": "v1.2.3 already exists."}
        result = {"repository": REPOSITORY, "tag": "v1.2.3", "commit": COMMIT, "checks": checks,
                  "failures": failures}
        return cli._tag_report(result)

    def test_failed_report_names_the_failure_and_forbids_the_tag(self):
        # Arrange
        failures = 1

        # Act
        text = self.report(failures)

        # Assert
        self.assertIn("FAIL  Tag unused", text)
        self.assertIn(f"1 of {len(publication.TAG_READINESS_CHECKS)} checks failed", text)
        self.assertIn("do not create the tag", text)
        self.assertNotIn("git tag -s", text)

    def test_passing_report_names_the_next_step_without_commands(self):
        # Arrange
        failures = 0

        # Act
        text = self.report(failures)

        # Assert
        self.assertIn(f"All {len(publication.TAG_READINESS_CHECKS)} checks passed", text)
        self.assertIn(f"signed tag v1.2.3 on {COMMIT}", text)
        self.assertNotIn("```sh", text)


class CurrentSignersTests(unittest.TestCase):
    def test_signers_are_read_from_the_authority_commit_and_checked_against_their_blob(self):
        # Arrange
        content = b'probe@example.invalid namespaces="git" ssh-ed25519 K\n'
        blob = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
        routes = []

        class Api:
            def request(self, route, *arguments, **keywords):
                routes.append(route)
                return {"type": "file", "path": ".github/allowed_signers", "encoding": "base64", "sha": blob,
                        "content": base64.b64encode(content).decode()}

        # Act
        signers = publication.current_signers(Api(), REPOSITORY, "m" * 40)

        # Assert
        self.assertEqual(content.decode(), signers)
        self.assertEqual([f"repos/{REPOSITORY}/contents/.github/allowed_signers?ref={'m' * 40}"], routes)

    def test_signers_that_differ_from_their_blob_fail(self):
        # Arrange
        class Api:
            def request(self, route, *arguments, **keywords):
                return {"type": "file", "path": ".github/allowed_signers", "encoding": "base64", "sha": "0" * 40,
                        "content": base64.b64encode(b"x").decode()}

        # Act
        with self.assertRaises(publication.GitHubError) as raised:
            publication.current_signers(Api(), REPOSITORY, "m" * 40)

        # Assert
        self.assertIn("differs from its Git blob", str(raised.exception))


class PreTagTests(unittest.TestCase):
    """Runs the local check in a real repository with real SSH keys and an isolated Git configuration."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.work = Path(directory.name).resolve()
        isolated = unittest.mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        isolated.start()
        self.addCleanup(isolated.stop)
        self.keys = {}
        for name in ("listed", "other"):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(self.work / name)],
                           check=True, timeout=60)
            self.keys[name] = " ".join((self.work / f"{name}.pub").read_text(encoding="ascii").split()[:2])

        self.root = self.work / "repository"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.root)], check=True, timeout=60)
        for key, value in (("user.name", "Probe"), ("user.email", "probe@example.invalid"),
                           ("commit.gpgsign", "false"), ("remote.origin.url", f"https://github.com/{REPOSITORY}")):
            self.git("config", key, value)
        (self.root / ".github").mkdir()
        (self.root / publication.ALLOWED_SIGNERS).write_text(
            f'probe@example.invalid namespaces="git" {self.keys["listed"]}\n', encoding="ascii")
        (self.root / "CHANGELOG.md").write_text("# Changelog\n\n## [1.2.3] - 2026-11-26\n\n- First.\n",
                                                encoding="ascii")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "init")
        self.commit = self.git("rev-parse", "HEAD")
        signers = self.work / "allowed_signers"
        signers.write_text(f'probe@example.invalid namespaces="git" {self.keys["listed"]}\n', encoding="ascii")
        self.signing = {"gpg.format": "ssh", "user.signingkey": str(self.work / "listed.pub"),
                        "gpg.ssh.allowedSignersFile": str(signers)}

    def git(self, *arguments, check=True):
        result = subprocess.run(["git", "-C", str(self.root), *arguments], capture_output=True, text=True,
                                timeout=120, check=False)
        if check and result.returncode:
            raise AssertionError(result.stderr)
        return result.stdout.strip() if check else result

    def run_check(self, *, signing=None, tag_taken=False, taken=False, qualified=True, immutable=True,
                  main_signers=None):
        main_signers = ((self.root / publication.ALLOWED_SIGNERS).read_text(encoding="ascii")
                        if main_signers is None else main_signers)
        for key, value in (self.signing if signing is None else signing).items():
            self.git("config", key, value)

        class Api:
            def request(self, route, *arguments, **keywords):
                assert route == f"repos/{REPOSITORY}/immutable-releases", route
                return {"enabled": immutable}

        def gates(root, repository, tree, source_ref, policy, verify=None):
            if not qualified:
                raise publication.qualification.QualificationError(f"No pull request into {source_ref} qualified "
                                                                   f"tree {tree} with .github/workflows/ci.yml.")
            return [{"name": "required / Required"}]

        policy = {"release": {"packages": ["Doka.Core"]}}
        with unittest.mock.patch.multiple(
                publication, observe_authority=lambda api, repository: {"policy": policy, "commit": "m" * 40},
                current_signers=lambda api, repository, commit: main_signers if commit == "m" * 40 else "",
                protected_head=lambda api, repository, ref: self.commit,
                tag_exists=lambda api, repository, tag: tag_taken,
                tags_on_commit=lambda root, commit: []), \
                unittest.mock.patch.object(publication.policies, "select_line"), \
                unittest.mock.patch.object(publication.qualification, "protected_gates", gates), \
                unittest.mock.patch.object(publication.nuget, "unused",
                                           return_value={"Doka.Core": "listed" if taken else "absent"}):
            return publication.pre_tag(Api(), self.root, "1.2.3")

    @staticmethod
    def failed(result):
        return {item["subject"]: item["detail"] for item in result["checks"] if not item["ok"]}

    def test_ready_checkout_passes_every_check_and_the_runbook_tag_verifies(self):
        # Arrange
        expected = list(publication.PRE_TAG_CHECKS)

        # Act
        result = self.run_check()
        self.git("tag", "-s", "v1.2.3", self.commit, "-m", "v1.2.3")
        verified = self.git("tag", "-v", "v1.2.3", check=False)

        # Assert
        self.assertEqual(expected, [item["subject"] for item in result["checks"]])
        self.assertEqual((0, "v1.2.3", self.commit), (result["failures"], result["tag"], result["commit"]))
        self.assertEqual(0, verified.returncode, verified.stderr)
        self.assertIn('Good "git" signature for probe@example.invalid', verified.stderr)

    def test_unlisted_signing_key_fails_both_signer_checks_and_git_tag_v(self):
        # Arrange
        signing = {**self.signing, "user.signingkey": str(self.work / "other.pub")}

        # Act
        result = self.run_check(signing=signing)
        self.git("tag", "-s", "v1.2.3", self.commit, "-m", "v1.2.3")
        verified = self.git("tag", "-v", "v1.2.3", check=False)

        # Assert
        self.assertEqual({"Signing key allowed", "Signer file for git tag -v"}, set(self.failed(result)))
        self.assertNotEqual(0, verified.returncode)
        self.assertIn("No principal matched", verified.stderr)

    def test_key_removed_from_main_fails_although_the_line_still_lists_it(self):
        # Arrange
        main_signers = f'probe@example.invalid namespaces="git" {self.keys["other"]}\n'

        # Act
        result = self.run_check(main_signers=main_signers)

        # Assert
        self.assertEqual({"Signing key allowed"}, set(self.failed(result)))
        self.assertIn("not listed in .github/allowed_signers of main", self.failed(result)["Signing key allowed"])

    def test_literal_signing_key_is_read(self):
        # Arrange
        signing = {**self.signing, "user.signingkey": f"key::{self.keys['listed']}"}

        # Act
        result = self.run_check(signing=signing)

        # Assert
        self.assertEqual({}, self.failed(result))

    def test_missing_signing_setup_names_each_setting(self):
        # Arrange
        signing = {"commit.gpgsign": "false"}

        # Act
        result = self.run_check(signing=signing)

        # Assert
        failed = self.failed(result)
        self.assertIn("gpg.format is not ssh.", failed["SSH signing format"])
        self.assertIn("user.signingkey is not set.", failed["Signing key configured"])
        self.assertTrue(failed["Signing key allowed"].startswith("Not checked: needs key"))
        self.assertTrue(failed["Signer file for git tag -v"].startswith("Not checked: needs key"))

    def test_uncommitted_file_fails_the_clean_checkout(self):
        # Arrange
        (self.root / "notes.txt").write_text("draft\n", encoding="ascii")

        # Act
        result = self.run_check()

        # Assert
        self.assertEqual({"Clean checkout"}, set(self.failed(result)))

    def test_independent_failures_are_all_reported(self):
        # Arrange
        expected = {"Immutable releases", "Tag unused", "Version unused on nuget.org", "Pull request qualification"}

        # Act
        result = self.run_check(tag_taken=True, taken=True, qualified=False, immutable=False)

        # Assert
        failed = self.failed(result)
        self.assertEqual(expected, set(failed))
        self.assertIn("Merge the change through a pull request", failed["Pull request qualification"])
        self.assertEqual(len(publication.PRE_TAG_CHECKS), len(result["checks"]))

    def test_missing_changelog_section_fails(self):
        # Arrange
        (self.root / "CHANGELOG.md").write_text("# Changelog\n", encoding="ascii")
        self.git("commit", "-q", "-am", "drop section")
        self.commit = self.git("rev-parse", "HEAD")

        # Act
        result = self.run_check()

        # Assert
        self.assertEqual({"Changelog section"}, set(self.failed(result)))


class PreTagReportTests(unittest.TestCase):
    def report(self, failures):
        from tools.release_publication import __main__ as cli

        checks = [{"subject": name, "ok": True, "detail": ""} for name in publication.PRE_TAG_CHECKS]
        if failures:
            checks[0] = {"subject": checks[0]["subject"], "ok": False, "detail": "Add the section."}
        return cli._pre_tag_report({"tag": "v1.2.3", "commit": COMMIT, "checks": checks, "failures": failures})

    def test_failed_report_forbids_the_start(self):
        # Arrange
        failures = 1

        # Act
        text = self.report(failures)

        # Assert
        self.assertIn("FAIL  Changelog section", text)
        self.assertIn(f"1 of {len(publication.PRE_TAG_CHECKS)} checks failed. Fix them before you start", text)

    def test_passing_report_names_the_start(self):
        # Arrange
        failures = 0

        # Act
        text = self.report(failures)

        # Assert
        self.assertIn(f"All {len(publication.PRE_TAG_CHECKS)} checks passed. Start the release candidate", text)


class PackageCheckCommandTests(unittest.TestCase):
    def test_pull_request_packages_get_the_candidate_checks_without_writes(self):
        # Arrange
        from tools.release_publication import __main__ as cli

        mock = unittest.mock
        root = Path(tempfile.mkdtemp())
        inventory = [{"id": "Doka.Core"}]
        api = RecordingFakeApi({})
        environment = {"GITHUB_REPOSITORY": REPOSITORY, "GITHUB_SHA": COMMIT}

        # Act
        with mock.patch.dict(cli.os.environ, environment, clear=True), \
                mock.patch.object(cli, "GitHub", return_value=api), \
                mock.patch.object(cli, "observe_authority", return_value={"policy": {"release": {}}}), \
                mock.patch.object(publication, "check_packages", return_value=(inventory, [])) as check, \
                mock.patch.object(cli.consumers, "generate", return_value={"Doka.Core": "ok"}) as generate:
            code = cli.main(["package-check", "--root", str(root), "--version", "1.2.3-ci.5", "--packages",
                             str(root / "packages"), "--probes", str(root / "probes.json"), "--tool",
                             str(root / "sbom-tool"), "--supplier", "Doka Labs", "--output", str(root / "logs")])

        # Assert
        self.assertEqual(0, code)
        self.assertEqual(("1.2.3-ci.5", REPOSITORY, COMMIT), check.call_args.args[3:6])
        self.assertEqual(inventory, generate.call_args.args[1])
        self.assertEqual([], api.calls)


if __name__ == "__main__":
    unittest.main()
