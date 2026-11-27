import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tools.release_lines import package_validation as validation


def state(properties=None, items=None):
    return {"properties": {"PackageId": "Doka.Core", "TargetFramework": "net10.0", **(properties or {})},
            "items": {name: [] for name in validation.ITEMS} | (items or {})}


class ToolArgumentTests(unittest.TestCase):
    def test_settings_map_to_the_options_of_the_tool(self):
        # Arrange
        current = state({"EnableStrictModeForBaselineValidation": "True", "RunApiCompat": "false",
                         "NoWarn": "CP0002;CS1591", "RoslynAssembliesPath": "/sdk/Roslyn/bincore"},
                        {"ApiCompatSuppressionFile": ["/repo/src/Core/CompatibilitySuppressions.xml"]})

        # Act
        arguments = validation.tool_arguments(current, Path("/p/Doka.Core.1.2.3.nupkg"),
                                              [".NETCoreApp,Version=v10.0|/a.dll,/b.dll"], Path("/b/core.1.0.0.nupkg"))

        # Assert
        self.assertEqual(["package", "/p/Doka.Core.1.2.3.nupkg",
                          "--run-api-compat", "false",
                          "--enable-strict-mode-for-baseline-validation", "true",
                          "--noWarn", "CP0002;CS1591",
                          "--roslyn-assemblies-path", "/sdk/Roslyn/bincore",
                          "--suppression-file", "/repo/src/Core/CompatibilitySuppressions.xml",
                          "--baseline-package", "/b/core.1.0.0.nupkg",
                          "--package-assembly-references", ".NETCoreApp,Version=v10.0|/a.dll,/b.dll"], arguments)

    def test_empty_settings_keep_the_defaults_of_the_tool(self):
        # Arrange
        current = state()

        # Act
        arguments = validation.tool_arguments(current, Path("/p/x.nupkg"), [], None)

        # Assert
        self.assertEqual(["package", "/p/x.nupkg"], arguments)

    def test_boolean_that_is_not_true_or_false_fails(self):
        # Arrange
        current = state({"ApiCompatRespectInternals": "yes"})

        # Act
        with self.assertRaises(validation.PackageValidationError) as raised:
            validation.tool_arguments(current, Path("/p/x.nupkg"), [], None)

        # Assert
        self.assertIn("ApiCompatRespectInternals must be true or false", str(raised.exception))

    def test_generating_suppression_files_fails(self):
        # Arrange
        current = state({"ApiCompatGenerateSuppressionFile": "true"})

        # Act
        with self.assertRaises(validation.PackageValidationError) as raised:
            validation.tool_arguments(current, Path("/p/x.nupkg"), [], None)

        # Assert
        self.assertIn("cannot generate suppression files", str(raised.exception))


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "src" / "Core" / "Core.csproj"
        self.project.parent.mkdir(parents=True)
        self.reference = self.root / "ref" / "Other.dll"
        self.reference.parent.mkdir()
        self.reference.touch()
        self.commands = []

    def run_fake(self, paths, platform=""):
        def run(command, **options):
            self.commands.append(command)
            items = [{"Identity": "net10.0", "TargetFrameworkMoniker": ".NETCoreApp,Version=v10.0",
                      "TargetPlatformMoniker": platform, "ReferencePath": ",".join(paths)}]
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                {"TargetResults": {"GetReferencesForApiCompatValidatePackage": {"Result": "Success",
                                                                               "Items": items}}}))
        return run

    def test_references_of_each_framework_use_the_monikers_of_the_inner_build(self):
        # Arrange
        current = state({"TargetFrameworks": "net10.0;net10.0-windows"})

        # Act
        arguments = validation.references(self.root, self.project, {"Configuration": "Release"}, current,
                                          run=self.run_fake([str(self.reference)], "Windows,Version=7.0"))

        # Assert
        self.assertEqual(2, len(self.commands))
        self.assertIn("-property:TargetFramework=net10.0-windows", self.commands[1])
        self.assertIn("-property:BuildProjectReferences=false", self.commands[0])
        self.assertEqual(f".NETCoreApp,Version=v10.0+Windows,Version=7.0|{self.reference}", arguments[0])

    def test_project_without_references_skips_them(self):
        # Arrange
        current = state({"RunPackageValidationWithoutReferences": "true"})

        # Act
        arguments = validation.references(self.root, self.project, {}, current, run=self.run_fake([]))

        # Assert
        self.assertEqual([], arguments)
        self.assertEqual([], self.commands)

    def test_missing_reference_fails_before_the_tool_runs(self):
        # Arrange
        current = state()

        # Act
        with self.assertRaises(validation.PackageValidationError) as raised:
            validation.references(self.root, self.project, {}, current,
                                  run=self.run_fake([str(self.root / "ref" / "Missing.dll")]))

        # Assert
        self.assertIn("missing after the build", str(raised.exception))


class BaselineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "global.json").write_text("{}", encoding="ascii")
        self.urls, self.verified = [], []

    def fetch(self, url, headers, timeout):
        self.urls.append(url)
        if url.endswith("index.json"):
            return 200, json.dumps({"resources": [{"@type": "PackageBaseAddress/3.0.0",
                                                   "@id": "https://api.nuget.org/v3-flatcontainer/"}]}).encode()

        return 200, b"baseline"

    def test_baseline_version_downloads_and_verifies_the_package_of_the_package_id(self):
        # Arrange
        current = state({"PackageValidationBaselineVersion": "1.0.0"})

        # Act
        path = validation.baseline(current, self.root / "baseline", self.root, fetcher=self.fetch,
                                   verifier=lambda path, timeout: self.verified.append(path))

        # Assert
        self.assertEqual("https://api.nuget.org/v3-flatcontainer/doka.core/1.0.0/doka.core.1.0.0.nupkg",
                         self.urls[-1])
        self.assertEqual([path], self.verified)
        self.assertEqual(b"baseline", path.read_bytes())

    def test_disabled_baseline_is_not_downloaded(self):
        # Arrange
        current = state({"PackageValidationBaselineVersion": "1.0.0", "DisablePackageBaselineValidation": "true"})

        # Act
        path = validation.baseline(current, self.root / "baseline", self.root, fetcher=self.fetch)

        # Assert
        self.assertIsNone(path)
        self.assertEqual([], self.urls)

    def test_baseline_path_without_version_is_not_skipped(self):
        # Arrange
        current = state({"PackageValidationBaselinePath": "/x.nupkg"})

        # Act
        with self.assertRaises(validation.PackageValidationError) as raised:
            validation.baseline(current, self.root / "baseline", self.root, fetcher=self.fetch)

        # Assert
        self.assertIn("PackageValidationBaselinePath is not supported", str(raised.exception))
        self.assertEqual([], self.urls)

    def test_disabled_baseline_ignores_a_baseline_path(self):
        # Arrange
        current = state({"PackageValidationBaselinePath": "/x.nupkg", "DisablePackageBaselineValidation": "true"})

        # Act
        path = validation.baseline(current, self.root / "baseline", self.root, fetcher=self.fetch)

        # Assert
        self.assertIsNone(path)

    def test_no_baseline_without_path_or_version(self):
        # Arrange
        current = state()

        # Act
        path = validation.baseline(current, self.root / "baseline", self.root, fetcher=self.fetch)

        # Assert
        self.assertIsNone(path)
        self.assertEqual([], self.urls)

    def test_baseline_path_is_not_supported(self):
        # Arrange
        current = state({"PackageValidationBaselineVersion": "1.0.0", "PackageValidationBaselinePath": "/x.nupkg"})

        # Act
        with self.assertRaises(validation.PackageValidationError) as raised:
            validation.baseline(current, self.root / "baseline", self.root, fetcher=self.fetch)

        # Assert
        self.assertIn("PackageValidationBaselinePath is not supported", str(raised.exception))


class InstallTests(unittest.TestCase):
    def test_tool_of_the_sdk_version_installs_from_the_verified_archive_only(self):
        # Arrange
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "global.json").write_text("{}", encoding="ascii")
            commands = []

            def run(command, **options):
                commands.append(command)
                if command[:2] == ["dotnet", "--version"]:
                    return SimpleNamespace(returncode=0, stdout="10.0.404\n")
                tool = Path(command[command.index("--tool-path") + 1]) / "apicompat"
                tool.parent.mkdir(parents=True)
                tool.touch()
                return SimpleNamespace(returncode=0, stdout="")

            fetch = BaselineTests.fetch.__get__(SimpleNamespace(urls=[]))

            # Act
            tool = validation.install_tool(root, root / "work", run=run, fetcher=fetch, verifier=lambda p, t: None)

            # Assert
            install = commands[1]
            self.assertEqual(["dotnet", "tool", "install", validation.TOOL, "--version", "10.0.404"], install[:6])
            config = Path(install[install.index("--configfile") + 1]).read_text(encoding="utf-8")
            self.assertIn("<clear />", config)
            self.assertTrue(tool.is_file())


if __name__ == "__main__":
    unittest.main()
