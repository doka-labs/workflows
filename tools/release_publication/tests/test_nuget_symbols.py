import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[3]
TOOL = ROOT / "tools" / "NuGetSymbols" / "NuGetSymbols.csproj"


def run(command, cwd):
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=600, check=False)


class NuGetSymbolsTests(unittest.TestCase):
    """Packs a real library with symbols and runs the C# tool on it, as the release candidate workflow does."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.work = Path(cls.directory.name)
        library = cls.work / "Probe.Library"
        library.mkdir()
        (library / "Probe.Library.csproj").write_text(
            '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net10.0</TargetFramework>'
            "<IncludeSymbols>true</IncludeSymbols><SymbolPackageFormat>snupkg</SymbolPackageFormat>"
            "<Version>1.0.0</Version><DebugType>portable</DebugType></PropertyGroup></Project>", encoding="ascii")
        (library / "Probe.cs").write_text("namespace Probe; public static class Value { public static int One => 1; }",
                                          encoding="ascii")
        cls.packages = cls.work / "packages"
        packed = run(["dotnet", "pack", str(library), "-c", "Release", "-o", str(cls.packages), "-nologo"], ROOT)
        if packed.returncode:
            raise AssertionError(packed.stdout + packed.stderr)

        built = run(["dotnet", "build", str(TOOL), "-c", "Release", "-nologo"], ROOT)
        if built.returncode:
            raise AssertionError(built.stdout + built.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def probe(self, packages):
        output = self.work / "probes.json"
        result = run(["dotnet", "run", "--no-build", "--project", str(TOOL), "-c", "Release", "--",
                      "--packages", str(packages), "--output", str(output)], ROOT)
        return result, (json.loads(output.read_text(encoding="utf-8")) if result.returncode == 0 else None)

    def rewrite_pdb(self, name, transform):
        packages = self.work / name
        packages.mkdir()
        for source in self.packages.iterdir():
            target = packages / source.name
            if source.suffix != ".snupkg":
                target.write_bytes(source.read_bytes())
                continue

            with zipfile.ZipFile(source) as original, zipfile.ZipFile(target, "w") as copy:
                for entry in original.infolist():
                    data = original.read(entry)
                    copy.writestr(entry, transform(data) if entry.filename.endswith(".pdb") else data)

        return packages

    def test_packed_library_yields_one_probe(self):
        # Arrange
        packages = self.packages

        # Act
        result, document = self.probe(packages)

        # Assert
        self.assertEqual(0, result.returncode, result.stderr)
        probe = document["probes"][0]
        self.assertEqual(("lib/net10.0/Probe.Library.dll", "lib/net10.0/Probe.Library.pdb"),
                         (probe["assembly"], probe["pdb"]))
        self.assertRegex(probe["url"], r"^https://symbols\.nuget\.org/download/symbols/Probe\.Library\.pdb/"
                                       r"[0-9a-f]{32}FFFFFFFF/Probe\.Library\.pdb$")
        self.assertRegex(probe["checksum"], r"^SHA256:[0-9a-f]{64}$")

    def test_corrupted_pdb_fails(self):
        # Arrange
        damaged = self.rewrite_pdb("damaged", lambda data: data[:-1] + bytes([data[-1] ^ 1]))

        # Act
        result, _ = self.probe(damaged)

        # Assert
        self.assertEqual(1, result.returncode)
        self.assertIn("does not match the checksum in its assembly", result.stderr)

    def test_pdb_with_same_guid_and_different_stamp_fails(self):
        # Arrange
        result, document = self.probe(self.packages)
        self.assertEqual(0, result.returncode, result.stderr)
        key = document["probes"][0]["url"].split("/")[-2]
        guid = UUID(hex=key[:32]).bytes_le

        def change_stamp(data):
            self.assertEqual(1, data.count(guid))
            # WHY: The stamp follows the GUID in the PDB ID. The checksum zeros all 20 ID bytes.
            # https://github.com/dotnet/runtime/blob/main/docs/design/specs/PE-COFF.md#portable-pdb-checksum
            stamp = data.index(guid) + len(guid)
            return data[:stamp] + bytes([data[stamp] ^ 1]) + data[stamp + 1:]

        damaged = self.rewrite_pdb("wrong-stamp", change_stamp)

        # Act
        result, _ = self.probe(damaged)

        # Assert
        self.assertEqual(1, result.returncode)
        self.assertIn("does not match the identity in its assembly", result.stderr)

    def test_missing_symbol_package_fails(self):
        # Arrange
        lonely = self.work / "lonely"
        lonely.mkdir(exist_ok=True)
        for source in self.packages.glob("*.nupkg"):
            (lonely / source.name).write_bytes(source.read_bytes())

        # Act
        result, _ = self.probe(lonely)

        # Assert
        self.assertEqual(1, result.returncode)
        self.assertIn("has no matching .snupkg", result.stderr)


class InternalsPinTests(unittest.TestCase):
    """Packs a library that grants its internals to a second packed library, as Tabular's core and EF packages."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        # WHY: On macOS the temporary directory lies under the /var symlink; packed from there, NuGet left the
        # project reference out of the nuspec, so the work directory is used by its resolved path.
        cls.work = Path(cls.directory.name).resolve()
        cls.packages = {grant: cls.pack(grant) for grant in (True, False)}
        built = run(["dotnet", "build", str(TOOL), "-c", "Release", "-nologo"], ROOT)
        if built.returncode:
            raise AssertionError(built.stdout + built.stderr)

    @classmethod
    def pack(cls, grant):
        root = cls.work / ("granted" if grant else "public")
        friend = '<ItemGroup><InternalsVisibleTo Include="Probe.Extension" /></ItemGroup>' if grant else ""
        properties = ("<PropertyGroup><TargetFramework>net10.0</TargetFramework><IncludeSymbols>true</IncludeSymbols>"
                      "<SymbolPackageFormat>snupkg</SymbolPackageFormat><Version>1.0.0</Version></PropertyGroup>")
        for name, body, extra in (
                ("Probe.Core", "namespace Probe; public static class Core { internal static int One => 1; }", friend),
                ("Probe.Extension", "namespace Probe; public static class Extension { public static int Two => 2; }",
                 '<ItemGroup><ProjectReference Include="../Probe.Core/Probe.Core.csproj" /></ItemGroup>')):
            project = root / name
            project.mkdir(parents=True)
            (project / f"{name}.csproj").write_text(f'<Project Sdk="Microsoft.NET.Sdk">{properties}{extra}</Project>',
                                                    encoding="ascii")
            (project / f"{name}.cs").write_text(body, encoding="ascii")

        for name in ("Probe.Core", "Probe.Extension"):
            packed = run(["dotnet", "pack", str(root / name), "-c", "Release", "-o", str(root / "packages"), "-nologo"],
                         ROOT)
            if packed.returncode:
                raise AssertionError(packed.stdout + packed.stderr)

        return root / "packages"

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def check(self, packages):
        return run(["dotnet", "run", "--no-build", "--project", str(TOOL), "-c", "Release", "--",
                    "--packages", str(packages), "--output", str(self.work / "probes.json")], ROOT)

    def with_range(self, name, packages, version_range):
        target = self.work / name
        target.mkdir()
        for source in packages.iterdir():
            if source.name != "Probe.Extension.1.0.0.nupkg":
                (target / source.name).write_bytes(source.read_bytes())
                continue

            with zipfile.ZipFile(source) as original, zipfile.ZipFile(target / source.name, "w") as copy:
                for entry in original.infolist():
                    data = original.read(entry)
                    if entry.filename.endswith(".nuspec"):
                        self.assertEqual(1, data.count(b'id="Probe.Core" version="1.0.0"'))
                        data = data.replace(b'id="Probe.Core" version="1.0.0"',
                                            f'id="Probe.Core" version="{version_range}"'.encode("ascii"))

                    copy.writestr(entry, data)

        return target

    def test_granted_internals_with_a_minimum_version_fail(self):
        # Arrange
        packages = self.packages[True]

        # Act
        result = self.check(packages)

        # Assert
        self.assertEqual(1, result.returncode)
        self.assertIn("Probe.Extension depends on Probe.Core with the range 1.0.0, but Probe.Core grants it its "
                      "internals; the dependency needs the exact pin [1.0.0].", result.stderr)

    def test_granted_internals_with_an_open_range_fail(self):
        # Arrange
        packages = self.with_range("open", self.packages[True], "[1.0.0, 2.0.0)")

        # Act
        result = self.check(packages)

        # Assert
        self.assertEqual(1, result.returncode)
        self.assertIn("needs the exact pin [1.0.0]", result.stderr)

    def test_granted_internals_with_the_exact_pin_pass(self):
        # Arrange
        packages = self.with_range("exact", self.packages[True], "[1.0.0]")

        # Act
        result = self.check(packages)

        # Assert
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Verified 1 exact pins", result.stdout)

    def test_dependency_without_granted_internals_keeps_its_range(self):
        # Arrange
        packages = self.packages[False]

        # Act
        result = self.check(packages)

        # Assert
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Verified 0 exact pins", result.stdout)


if __name__ == "__main__":
    unittest.main()
