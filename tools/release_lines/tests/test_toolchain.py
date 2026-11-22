import json
import tempfile
import unittest
from pathlib import Path

from tools.release_lines import toolchain

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "release-policy.json"


def policy(coupling="coupled", major=11, stage="ga", expected=2, prefix="src/Directory.Build.props"):
    value = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    value["lines"] = [{"sourceRef": "refs/heads/main", "role": "main", "packageMajor": major,
                       "channel": "stable-and-prerelease", "upstreamStage": stage, "state": "active"}]
    value["toolchain"].update(coupling=coupling, expectedLockfiles=expected, versionPrefix=prefix)
    return value


def lock(framework_sections):
    return json.dumps({"version": 2, "dependencies": framework_sections})


def entry(version, kind="Direct"):
    return {"type": kind, "resolved": version}


class Repository:
    def __init__(self, directory, *, sdk="11.0.100", prerelease=False, frameworks="<TargetFramework>net11.0</TargetFramework>",
                 packages='<PackageVersion Include="Microsoft.EntityFrameworkCore" Version="11.0.2" />',
                 prefix="11.1.0", locks=None, runner=None):
        self.root = Path(directory)
        document = {"sdk": {"version": sdk, "rollForward": "disable", "allowPrerelease": prerelease}}
        if runner:
            document["test"] = {"runner": runner}

        self.write("global.json", json.dumps(document))
        self.write("Directory.Build.props", f"<Project><PropertyGroup>{frameworks}</PropertyGroup></Project>")
        self.write("Directory.Packages.props", f"<Project><ItemGroup>{packages}</ItemGroup></Project>")
        self.write("src/Directory.Build.props", f"<Project><PropertyGroup><VersionPrefix>{prefix}</VersionPrefix>"
                                                "</PropertyGroup></Project>")
        default = {"net11.0": {"Microsoft.EntityFrameworkCore": entry("11.0.2"),
                               "Microsoft.EntityFrameworkCore.Relational": entry("11.0.2", "Transitive")}}
        for name, sections in (locks or {"A": default, "B": default}).items():
            self.write(f"src/{name}/{name}.csproj", "<Project />")
            self.write(f"src/{name}/packages.lock.json", lock(sections))

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class CoupledTests(unittest.TestCase):
    def check(self, value=None, **repository):
        with tempfile.TemporaryDirectory() as directory:
            Repository(directory, **repository)
            return toolchain.check(Path(directory), value or policy(), "refs/heads/main")

    def failure(self, value=None, **repository):
        with self.assertRaises(toolchain.ToolchainError) as raised:
            self.check(value, **repository)

        return str(raised.exception)

    def test_matching_toolchain_passes(self):
        # Arrange
        runner = "Microsoft.Testing.Platform"

        # Act
        result = self.check(runner=runner)

        # Assert
        self.assertEqual((["net11.0"], runner, 2), (result["targetFrameworks"], result["testRunner"],
                                                    result["lockfiles"]))

    def test_sdk_of_other_major_fails(self):
        # Arrange
        sdk = "10.0.404"

        # Act
        message = self.failure(sdk=sdk)

        # Assert
        self.assertIn("not major 11", message)

    def test_prerelease_sdk_flag_must_match_stage(self):
        # Arrange
        prerelease = True

        # Act
        message = self.failure(prerelease=prerelease)

        # Assert
        self.assertIn("allowPrerelease must be false", message)

    def test_several_frameworks_fail_when_coupled(self):
        # Arrange
        frameworks = "<TargetFrameworks>net10.0;net11.0</TargetFrameworks>"

        # Act
        message = self.failure(frameworks=frameworks)

        # Assert
        self.assertIn("exactly one framework", message)

    def test_framework_of_other_major_fails(self):
        # Arrange
        frameworks = "<TargetFramework>net10.0</TargetFramework>"

        # Act
        message = self.failure(frameworks=frameworks)

        # Assert
        self.assertIn("does not match package major 11", message)

    def test_interval_crossing_the_major_fails(self):
        # Arrange
        packages = '<PackageVersion Include="Microsoft.EntityFrameworkCore" Version="[11.0.0, 12.0.1)" />'

        # Act
        message = self.failure(packages=packages)

        # Assert
        self.assertIn("inside major 11", message)

    def test_interval_up_to_next_major_passes(self):
        # Arrange
        packages = '<PackageVersion Include="Microsoft.EntityFrameworkCore" Version="[11.0.0, 12.0.0)" />'

        # Act
        result = self.check(packages=packages)

        # Assert
        self.assertEqual(["net11.0"], result["targetFrameworks"])

    def test_interval_on_rc_line_fails(self):
        # Arrange
        packages = '<PackageVersion Include="Microsoft.EntityFrameworkCore" Version="[11.0.0-rc.1, 13.0.0)" />'

        value = policy()
        value["lines"].append({"sourceRef": "refs/heads/preview/12.x", "role": "preview", "packageMajor": 12,
                               "channel": "prerelease", "upstreamStage": "rc", "state": "active"})
        with tempfile.TemporaryDirectory() as directory:
            Repository(directory, sdk="12.0.100-rc.1", prerelease=True, packages=packages.replace("11.", "12."),
                       frameworks="<TargetFramework>net12.0</TargetFramework>", prefix="12.0.0")

            # Act
            with self.assertRaises(toolchain.ToolchainError) as raised:
                toolchain.check(Path(directory), value, "refs/heads/preview/12.x")

        message = str(raised.exception)

        # Assert
        self.assertIn("require an exact version", message)

    def test_property_reference_resolves_from_default(self):
        # Arrange
        packages = ('<PackageVersion Include="Microsoft.EntityFrameworkCore" Version="$(EfVersion)" />'
                    "</ItemGroup><PropertyGroup><EfVersion Condition=\"'$(EfVersion)' == ''\">10.0.1</EfVersion>"
                    "</PropertyGroup><ItemGroup>")

        # Act
        message = self.failure(packages=packages)

        # Assert
        self.assertIn("10.0.1 does not belong to major 11", message)

    def test_unsupported_condition_fails(self):
        # Arrange
        packages = ('<PackageVersion Include="Microsoft.EntityFrameworkCore" Version="11.0.2" '
                    "Condition=\"'$(Configuration)' == 'Release'\" />")

        # Act
        message = self.failure(packages=packages)

        # Assert
        self.assertIn("unsupported condition", message)

    def test_project_override_applies_to_its_project(self):
        # Arrange
        packages = ('<PackageVersion Include="Microsoft.EntityFrameworkCore" Version="11.0.2" /></ItemGroup>'
                    "<ItemGroup Condition=\"'$(MSBuildProjectName)' == 'B'\">"
                    '<PackageVersion Update="Microsoft.EntityFrameworkCore" Version="[11.0.1, 12.0.0)" />')
        locks = {"A": {"net11.0": {"Microsoft.EntityFrameworkCore": entry("11.0.2")}},
                 "B": {"net11.0": {"Microsoft.EntityFrameworkCore": entry("11.0.1")}}}

        # Act
        result = self.check(packages=packages, locks=locks)

        # Assert
        self.assertEqual(2, result["lockfiles"])

    def test_direct_resolution_outside_declaration_fails(self):
        # Arrange
        locks = {"A": {"net11.0": {"Microsoft.EntityFrameworkCore": entry("11.0.3")}},
                 "B": {"net11.0": {"Microsoft.EntityFrameworkCore": entry("11.0.2")}}}

        # Act
        message = self.failure(locks=locks)

        # Assert
        self.assertIn("11.0.3 is outside its declared version", message)

    def test_unpinned_central_transitive_resolution_is_checked_by_major_only(self):
        # Arrange
        locks = {"A": {"net11.0": {"Microsoft.EntityFrameworkCore": entry("11.0.1", "CentralTransitive")}},
                 "B": {"net11.0": {"Microsoft.EntityFrameworkCore": entry("11.0.2")}}}

        # Act
        result = self.check(locks=locks)

        # Assert
        self.assertEqual(2, result["lockfiles"])

    def test_pinned_central_transitive_resolution_must_match(self):
        # Arrange
        frameworks = ("<TargetFramework>net11.0</TargetFramework>"
                      "<CentralPackageTransitivePinningEnabled>true</CentralPackageTransitivePinningEnabled>")
        locks = {"A": {"net11.0": {"Microsoft.EntityFrameworkCore": entry("11.0.1", "CentralTransitive")}},
                 "B": {"net11.0": {"Microsoft.EntityFrameworkCore": entry("11.0.2")}}}

        # Act
        message = self.failure(frameworks=frameworks, locks=locks)

        # Assert
        self.assertIn("11.0.1 is outside its declared version", message)

    def test_transitive_package_of_other_major_fails(self):
        # Arrange
        locks = {"A": {"net11.0": {"Microsoft.EntityFrameworkCore": entry("11.0.2"),
                                   "Microsoft.EntityFrameworkCore.Abstractions": entry("12.0.0", "Transitive")}},
                 "B": {"net11.0": {"Microsoft.EntityFrameworkCore": entry("11.0.2")}}}

        # Act
        message = self.failure(locks=locks)

        # Assert
        self.assertIn("12.0.0 does not fit net11.0", message)

    def test_mixed_patches_in_one_lockfile_fail(self):
        # Arrange
        locks = {"A": {"net11.0": {"Microsoft.EntityFrameworkCore": entry("11.0.2"),
                                   "Microsoft.EntityFrameworkCore.Relational": entry("11.0.1", "Transitive")}},
                 "B": {"net11.0": {"Microsoft.EntityFrameworkCore": entry("11.0.2")}}}

        # Act
        message = self.failure(locks=locks)

        # Assert
        self.assertIn("src/A/packages.lock.json: coherent group 1", message)

    def test_project_references_are_skipped(self):
        # Arrange
        locks = {"A": {"net11.0": {"Microsoft.EntityFrameworkCore": entry("11.0.2"),
                                   "microsoft.entityframeworkcore.local": {"type": "Project"}}},
                 "B": {"net11.0": {"Microsoft.EntityFrameworkCore": entry("11.0.2")}}}

        # Act
        result = self.check(locks=locks)

        # Assert
        self.assertEqual(2, result["lockfiles"])

    def test_missing_lockfile_fails(self):
        # Arrange
        value = policy(expected=3)

        # Act
        message = self.failure(value)

        # Assert
        self.assertIn("Expected 3 regular lockfiles, found 2", message)

    def test_lockfile_section_for_other_framework_fails(self):
        # Arrange
        locks = {"A": {"net10.0": {"Microsoft.EntityFrameworkCore": entry("10.0.2")}},
                 "B": {"net11.0": {"Microsoft.EntityFrameworkCore": entry("11.0.2")}}}

        # Act
        message = self.failure(locks=locks)

        # Assert
        self.assertIn("section for net10.0", message)

    def test_prerelease_version_prefix_fails(self):
        # Arrange
        prefix = "11.1.0-rc.1"

        # Act
        message = self.failure(prefix=prefix)

        # Assert
        self.assertIn("stable 11.x version", message)


class IndependentTests(unittest.TestCase):
    def repository(self, directory, **overrides):
        values = {"sdk": "11.0.100", "frameworks": "<TargetFrameworks>net10.0;net11.0</TargetFrameworks>",
                  "packages": ("</ItemGroup><ItemGroup Condition=\"'$(TargetFramework)' == 'net10.0'\">"
                               '<PackageVersion Include="Microsoft.EntityFrameworkCore" Version="10.0.15" />'
                               "</ItemGroup><ItemGroup Condition=\"'$(TargetFramework)' == 'net11.0'\">"
                               '<PackageVersion Include="Microsoft.EntityFrameworkCore" Version="11.0.2" />'),
                  "prefix": "2.3.0",
                  "locks": {"A": {"net10.0": {"Microsoft.EntityFrameworkCore": entry("10.0.15")},
                                  "net11.0": {"Microsoft.EntityFrameworkCore": entry("11.0.2")}}}}
        values.update(overrides)
        Repository(directory, **values)

    def test_each_framework_uses_its_own_major(self):
        # Arrange
        with tempfile.TemporaryDirectory() as directory:
            self.repository(directory)

            # Act
            result = toolchain.check(Path(directory), policy("independent", major=2, expected=1), "refs/heads/main")

        # Assert
        self.assertEqual(["net10.0", "net11.0"], result["targetFrameworks"])

    def test_sdk_must_match_highest_framework(self):
        # Arrange
        with tempfile.TemporaryDirectory() as directory:
            self.repository(directory, sdk="10.0.404")

            # Act
            with self.assertRaises(toolchain.ToolchainError) as raised:
                toolchain.check(Path(directory), policy("independent", major=2, expected=1), "refs/heads/main")

        # Assert
        self.assertIn("not major 11", str(raised.exception))

    def test_unconditional_framework_package_fails_for_several_frameworks(self):
        # Arrange
        packages = '<PackageVersion Include="Microsoft.EntityFrameworkCore" Version="11.0.2" />'
        with tempfile.TemporaryDirectory() as directory:
            self.repository(directory, packages=packages)

            # Act
            with self.assertRaises(toolchain.ToolchainError) as raised:
                toolchain.check(Path(directory), policy("independent", major=2, expected=1), "refs/heads/main")

        # Assert
        self.assertIn("for net10.0", str(raised.exception))

    def test_resolution_of_wrong_framework_major_fails(self):
        # Arrange
        locks = {"A": {"net10.0": {"Microsoft.EntityFrameworkCore": entry("11.0.2")},
                       "net11.0": {"Microsoft.EntityFrameworkCore": entry("11.0.2")}}}
        with tempfile.TemporaryDirectory() as directory:
            self.repository(directory, locks=locks)

            # Act
            with self.assertRaises(toolchain.ToolchainError) as raised:
                toolchain.check(Path(directory), policy("independent", major=2, expected=1), "refs/heads/main")

        # Assert
        self.assertIn("does not fit net10.0", str(raised.exception))


class PatternTests(unittest.TestCase):
    def test_prefix_pattern_matches_children_not_lookalikes(self):
        # Arrange
        patterns = ["Microsoft.EntityFrameworkCore.*"]

        # Act
        results = [toolchain.matches(name, patterns) for name in (
            "Microsoft.EntityFrameworkCore.Relational", "microsoft.entityframeworkcore.design",
            "Microsoft.EntityFrameworkCoreX", "Microsoft.EntityFrameworkCore")]

        # Assert
        self.assertEqual([True, True, False, False], results)


if __name__ == "__main__":
    unittest.main()
