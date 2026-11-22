import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

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
        damaged = self.work / "damaged"
        damaged.mkdir(exist_ok=True)
        for source in self.packages.iterdir():
            target = damaged / source.name
            if source.suffix != ".snupkg":
                target.write_bytes(source.read_bytes())
                continue

            with zipfile.ZipFile(source) as original, zipfile.ZipFile(target, "w") as copy:
                for entry in original.infolist():
                    data = original.read(entry)
                    copy.writestr(entry, data[:-1] + bytes([data[-1] ^ 1]) if entry.filename.endswith(".pdb") else data)

        # Act
        result, _ = self.probe(damaged)

        # Assert
        self.assertEqual(1, result.returncode)
        self.assertIn("does not match the checksum in its assembly", result.stderr)

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


if __name__ == "__main__":
    unittest.main()
