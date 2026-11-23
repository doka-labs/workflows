import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.change_scope import scope

POLICY = {"schemaVersion": 1, "documentation": ["docs/**", "*.md", "**/*.md"]}
PROJECT = ('<Project Sdk="Microsoft.NET.Sdk"><ItemGroup>'
           '<None Include="..\\..\\README.md" Pack="true" PackagePath="/" /></ItemGroup></Project>')


# WHY: The test repositories must not inherit signing, hooks or templates from the machine's Git configuration.
ISOLATED = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(root, *arguments):
    completed = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, text=True, check=True,
                               env=ISOLATED)
    return completed.stdout.strip()


class Repository:
    def __init__(self, policy=POLICY):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "test@example.invalid")
        git(self.root, "config", "user.name", "Test")
        if policy is not None:
            self.write(".github/change-scope.json", json.dumps(policy))

        self.write("src/Core/Core.csproj", PROJECT)
        self.write("src/Core/Code.cs", "class C { }")
        self.write("README.md", "# Readme")
        self.write("docs/guide.md", "Guide")
        self.base = self.commit()

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(self):
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "change", "--allow-empty")
        return git(self.root, "rev-parse", "HEAD")

    def classify(self):
        return scope.classify(self.root, self.base, self.commit())

    def close(self):
        self.directory.cleanup()


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.repository = Repository()
        self.addCleanup(self.repository.close)

    def test_documentation_change_is_docs(self):
        # Arrange
        self.repository.write("docs/guide.md", "Changed guide")

        # Act
        result = self.repository.classify()

        # Assert
        self.assertEqual("docs", result["scope"])

    def test_packed_readme_is_full(self):
        # Arrange
        self.repository.write("README.md", "# Changed readme")

        # Act
        result = self.repository.classify()

        # Assert
        self.assertEqual(("full", ["README.md"]), (result["scope"], result["nonDocumentation"]))

    def test_source_with_documentation_is_full(self):
        # Arrange
        self.repository.write("docs/guide.md", "Changed guide")
        self.repository.write("src/Core/Code.cs", "class D { }")

        # Act
        result = self.repository.classify()

        # Assert
        self.assertEqual(("full", ["src/Core/Code.cs"]), (result["scope"], result["nonDocumentation"]))

    def test_policy_change_is_full_even_when_listed(self):
        # Arrange
        self.repository.write(".github/change-scope.json", json.dumps(
            {"schemaVersion": 1, "documentation": ["**/*"]}))

        # Act
        result = self.repository.classify()

        # Assert
        self.assertEqual("full", result["scope"])

    def test_rename_out_of_documentation_is_full(self):
        # Arrange
        git(self.repository.root, "mv", "docs/guide.md", "src/Core/guide.md")

        # Act
        result = self.repository.classify()

        # Assert
        self.assertEqual(("full", ["src/Core/guide.md"]),
                         (result["scope"], [path for path in result["nonDocumentation"]]))

    def test_unlisted_path_is_full(self):
        # Arrange
        self.repository.write("LICENSE", "license")

        # Act
        result = self.repository.classify()

        # Assert
        self.assertEqual(("full", ["LICENSE"]), (result["scope"], result["nonDocumentation"]))

    def test_empty_change_is_full(self):
        # Arrange
        repository = self.repository

        # Act
        result = repository.classify()

        # Assert
        self.assertEqual("full", result["scope"])


class PolicyTests(unittest.TestCase):
    def test_missing_policy_makes_every_change_full(self):
        # Arrange
        repository = Repository(policy=None)
        self.addCleanup(repository.close)
        repository.write("docs/guide.md", "Changed guide")

        # Act
        result = repository.classify()

        # Assert
        self.assertEqual("full", result["scope"])

    def test_parent_directory_pattern_fails(self):
        # Arrange
        repository = Repository(policy={"schemaVersion": 1, "documentation": ["../docs/**"]})
        self.addCleanup(repository.close)

        # Act
        with self.assertRaises(scope.ScopeError) as raised:
            scope.load_policy(repository.root)

        # Assert
        self.assertIn("relative path patterns", str(raised.exception))


class PackedFileTests(unittest.TestCase):
    def test_unresolved_property_packs_every_file_of_that_name(self):
        # Arrange
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "src").mkdir()
        (root / "src" / "Directory.Build.props").write_text(
            '<Project><ItemGroup><None Include="$(RepositoryRoot)NOTICE.md" Pack="true" /></ItemGroup></Project>',
            encoding="utf-8")

        # Act
        inputs = scope.build_inputs(root)

        # Assert
        self.assertFalse(scope.is_documentation("docs/NOTICE.md", ["docs/**"], inputs))


class BuildInputTests(unittest.TestCase):
    def setUp(self):
        self.repository = Repository()
        self.addCleanup(self.repository.close)

    def test_embedded_documentation_glob_is_full(self):
        # Arrange
        self.repository.write("src/Directory.Build.props", '<Project><ItemGroup>'
                              '<EmbeddedResource Include="../docs/**/*.md" /></ItemGroup></Project>')
        self.repository.base = self.repository.commit()
        self.repository.write("docs/guide.md", "Changed guide")

        # Act
        result = self.repository.classify()

        # Assert
        self.assertEqual("full", result["scope"])

    def test_packed_root_readme_is_a_build_input(self):
        # Arrange
        root = self.repository.root

        # Act
        inputs = scope.build_inputs(root)

        # Assert
        self.assertEqual(({"README.md"}, {"src/Core"}), (inputs["paths"], inputs["directories"]))


class ProjectDirectoryPropertyTests(unittest.TestCase):
    def test_project_directory_readme_leaves_other_readmes_documentation(self):
        # Arrange
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "src").mkdir()
        (root / "src" / "Directory.Build.props").write_text(
            '<Project><ItemGroup><None Include="$(MSBuildProjectDirectory)/README.md" Pack="true" />'
            "</ItemGroup></Project>", encoding="utf-8")

        # Act
        inputs = scope.build_inputs(root)

        # Assert
        self.assertTrue(scope.is_documentation("docs/README.md", ["docs/**"], inputs))


class JudgeTests(unittest.TestCase):
    def test_documentation_change_may_skip_listed_jobs(self):
        # Arrange
        results = {"scope": {"result": "success"}, "tests": {"result": "skipped"}}

        # Act
        problems = scope.judge(results, "docs", ["tests"])

        # Assert
        self.assertEqual([], problems)

    def test_full_change_may_skip_nothing(self):
        # Arrange
        results = {"scope": {"result": "success"}, "tests": {"result": "skipped"}}

        # Act
        problems = scope.judge(results, "full", ["tests"])

        # Assert
        self.assertEqual(["tests ended with skipped."], problems)

    def test_unlisted_skipped_job_fails_for_documentation(self):
        # Arrange
        results = {"scope": {"result": "success"}, "docs-checks": {"result": "skipped"}}

        # Act
        problems = scope.judge(results, "docs", [])

        # Assert
        self.assertEqual(["docs-checks ended with skipped."], problems)

    def test_failure_fails_in_every_scope(self):
        # Arrange
        results = {"scope": {"result": "success"}, "tests": {"result": "failure"}}

        # Act
        problems = scope.judge(results, "docs", ["tests"])

        # Assert
        self.assertEqual(["tests ended with failure."], problems)

    def test_skippable_name_without_job_fails(self):
        # Arrange
        results = {"scope": {"result": "success"}}

        # Act
        problems = scope.judge(results, "docs", ["test"])

        # Assert
        self.assertEqual(["Skippable jobs are not needed by the gate: test."], problems)


class GateCommandTests(unittest.TestCase):
    def run_gate(self, results, scope_name, skippable):
        from unittest import mock

        from tools.change_scope import __main__ as cli

        environment = {"GATE_RESULTS": json.dumps(results), "GATE_SKIPPABLE": json.dumps(skippable)}
        with mock.patch.dict(os.environ, environment, clear=True):
            return cli.main(["gate", "--scope", scope_name])

    def test_documentation_change_with_skipped_tests_passes(self):
        # Arrange
        results = {"scope": {"result": "success"}, "tests": {"result": "skipped"}}

        # Act
        code = self.run_gate(results, "docs", ["tests"])

        # Assert
        self.assertEqual(0, code)

    def test_missing_scope_output_fails(self):
        # Arrange
        results = {"scope": {"result": "failure"}, "tests": {"result": "skipped"}}

        # Act
        code = self.run_gate(results, "", ["tests"])

        # Assert
        self.assertEqual(1, code)

    def test_classify_without_base_is_full(self):
        # Arrange
        from unittest import mock

        from tools.change_scope import __main__ as cli

        output = Path(tempfile.mkdtemp()) / "output"

        # Act
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}, clear=True):
            code = cli.main(["classify", "--root", ".", "--base", ""])

        # Assert
        self.assertEqual((0, "scope=full\n"), (code, output.read_text(encoding="ascii")))
