import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.release_publication import consumers, packages

ROOT = Path(__file__).resolve().parents[3]


def pack(directory, name, extra=""):
    project = directory / "src" / name
    project.mkdir(parents=True)
    (project / f"{name}.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net10.0</TargetFramework>'
        f"<Version>1.2.3</Version></PropertyGroup>{extra}</Project>", encoding="ascii")
    (project / "Code.cs").write_text(f"namespace {name.replace('.', '_')}; public static class C {{ }}",
                                     encoding="ascii")
    result = subprocess.run(["dotnet", "pack", str(project), "-c", "Release", "-o", str(directory / "packages"),
                             "-nologo"], capture_output=True, text=True, timeout=600, check=False)
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)


class RestoreTests(unittest.TestCase):
    """Restores real consumers of freshly packed packages from a local candidate feed."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        # WHY: On macOS the temp directory is reached through the /var -> /private/var symlink; packing from the
        # unresolved path dropped the ProjectReference dependency from Probe.Ext's nuspec.
        cls.work = Path(cls.directory.name).resolve()
        pack(cls.work, "Probe.Core")
        pack(cls.work, "Probe.Ext",
             '<ItemGroup><ProjectReference Include="../Probe.Core/Probe.Core.csproj" /></ItemGroup>')
        cls.archives = {name: cls.work / "packages" / f"{name}.1.2.3.nupkg" for name in ("Probe.Core", "Probe.Ext")}

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def restore(self, name, feed_archives):
        work = Path(tempfile.mkdtemp(dir=self.work))
        feed = work / "feed"
        feed.mkdir()
        for archive in feed_archives.values():
            shutil.copyfile(archive, feed / archive.name)

        consumer = consumers.restore_consumer(work / "run", name, "1.2.3", ["net10.0"], ROOT / "global.json", feed,
                                              list(self.archives), work / "restore.log")
        return work, json.loads((consumer / "obj/project.assets.json").read_text(encoding="utf-8"))

    def test_consumer_resolves_the_candidate_closure(self):
        # Arrange
        name = "Probe.Ext"

        # Act
        work, assets = self.restore(name, self.archives)
        resolved = consumers.closure(assets, name, "1.2.3", ["net10.0"])

        # Assert
        self.assertEqual({("probe.core", "1.2.3"), ("probe.ext", "1.2.3")}, resolved)

    def test_restored_candidate_bytes_match(self):
        # Arrange
        name = "Probe.Ext"
        work, assets = self.restore(name, self.archives)

        # Act
        result = consumers.require_candidate_bytes(assets, work / "run" / "packages", self.archives, "1.2.3")

        # Assert
        self.assertIsNone(result)

    def test_other_bytes_of_a_candidate_dependency_fail(self):
        # Arrange
        changed = self.work / "changed"
        changed.mkdir(exist_ok=True)
        core = changed / "Probe.Core.1.2.3.nupkg"
        with zipfile.ZipFile(self.archives["Probe.Core"]) as source, zipfile.ZipFile(core, "w") as target:
            for entry in source.infolist():
                target.writestr(entry, source.read(entry))

            target.writestr("extra.txt", b"x")

        work, assets = self.restore("Probe.Ext", self.archives)

        # Act
        with self.assertRaises(consumers.ConsumerError) as raised:
            consumers.require_candidate_bytes(assets, work / "run" / "packages", {**self.archives, "Probe.Core": core},
                                              "1.2.3")

        # Assert
        self.assertIn("Probe.Core has other bytes", str(raised.exception))


def assets(libraries, dependencies=None, framework="net10.0", references=None):
    return {"targets": {framework: {}},
            "project": {"frameworks": {framework: {"dependencies": dependencies or {"Doka.Core": {"version": "[1.2.3, 1.2.3]"}}}},
                        "restore": {"frameworks": {framework: {"projectReferences": references or {}}}}},
            "libraries": {key: {"type": "package"} for key in libraries}}


class ClosureTests(unittest.TestCase):
    def test_test_tooling_in_the_graph_fails(self):
        # Arrange
        value = assets(["Doka.Core/1.2.3", "xunit.v3/4.0.3"])

        # Act
        with self.assertRaises(consumers.ConsumerError) as raised:
            consumers.closure(value, "Doka.Core", "1.2.3", ["net10.0"])

        # Assert
        self.assertIn("Test or build tooling leaked", str(raised.exception))

    def test_range_reference_fails(self):
        # Arrange
        value = assets(["Doka.Core/1.2.3"], {"Doka.Core": {"version": "[1.2.3, )"}})

        # Act
        with self.assertRaises(consumers.ConsumerError) as raised:
            consumers.closure(value, "Doka.Core", "1.2.3", ["net10.0"])

        # Assert
        self.assertIn("exact version", str(raised.exception))

    def test_project_reference_fails(self):
        # Arrange
        value = assets(["Doka.Core/1.2.3"], references={"x.csproj": {}})

        # Act
        with self.assertRaises(consumers.ConsumerError) as raised:
            consumers.closure(value, "Doka.Core", "1.2.3", ["net10.0"])

        # Assert
        self.assertIn("project references", str(raised.exception))


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.archive = Path(self.directory.name) / "Doka.Core.1.2.3.nupkg"
        self.archive.write_bytes(b"package")

    def tearDown(self):
        self.directory.cleanup()

    def manifest(self, components):
        packages_list = [{"SPDXID": "SPDXRef-RootPackage", "name": "Doka.Core", "versionInfo": "1.2.3"}]
        packages_list += [{"SPDXID": f"SPDXRef-{name}", "name": name, "versionInfo": version,
                           "externalRefs": [{"referenceType": "purl", "referenceLocator": f"pkg:nuget/{name}@{version}"}]}
                          for name, version in components]
        return {"spdxVersion": "SPDX-2.2", "documentDescribes": ["SPDXRef-RootPackage"], "packages": packages_list,
                "files": [{"fileName": f"./{self.archive.name}",
                           "checksums": [{"algorithm": "SHA256", "checksumValue": packages.sha256(self.archive)}]}]}

    def test_exact_closure_passes(self):
        # Arrange
        manifest = self.manifest([("Microsoft.Extensions.Logging.Abstractions", "10.0.0")])
        resolved = {("microsoft.extensions.logging.abstractions", "10.0.0")}

        # Act
        result = consumers.verify_manifest(manifest, self.archive, resolved, "Doka.Core", "1.2.3")

        # Assert
        self.assertIsNone(result)

    def test_missing_component_fails(self):
        # Arrange
        manifest = self.manifest([])
        resolved = {("microsoft.extensions.logging.abstractions", "10.0.0")}

        # Act
        with self.assertRaises(consumers.ConsumerError) as raised:
            consumers.verify_manifest(manifest, self.archive, resolved, "Doka.Core", "1.2.3")

        # Assert
        self.assertIn("missing", str(raised.exception))

    def test_other_archive_digest_fails(self):
        # Arrange
        manifest = self.manifest([])
        manifest["files"][0]["checksums"][0]["checksumValue"] = "0" * 64

        # Act
        with self.assertRaises(consumers.ConsumerError) as raised:
            consumers.verify_manifest(manifest, self.archive, set(), "Doka.Core", "1.2.3")

        # Assert
        self.assertIn("exactly the candidate archive", str(raised.exception))

    def test_validation_without_validated_file_fails(self):
        # Arrange
        report = {"Result": "Success", "ValidationErrors": {"Count": 0},
                  "Summary": {"ValidationTelemetery": {"TotalFilesInManifest": 1, "FilesSuccessfulCount": 0,
                                                       "FilesValidatedCount": 0, "FilesFailedCount": 0,
                                                       "FilesSkippedCount": 1}}}

        # Act
        with self.assertRaises(consumers.ConsumerError) as raised:
            consumers.verify_validation(report)

        # Assert
        self.assertIn("exactly the candidate archive", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
