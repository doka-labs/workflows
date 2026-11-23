import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path, PurePosixPath

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
        # The path is relative to the importing project src/Core, as MSBuild resolves it.
        self.repository.write("src/Directory.Build.props", '<Project><ItemGroup>'
                              '<EmbeddedResource Include="../../docs/**/*.md" /></ItemGroup></Project>')
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


class ProjectDirectoryEscapeTests(unittest.TestCase):
    def setUp(self):
        self.repository = Repository()
        self.addCleanup(self.repository.close)

    def classify_with(self, include, changed):
        self.repository.write("src/Directory.Build.props",
                              f'<Project><ItemGroup><AdditionalFiles Include="{include}" /></ItemGroup></Project>')
        self.repository.write(changed, "rules")
        self.repository.base = self.repository.commit()
        self.repository.write(changed, "changed rules")
        return self.repository.classify()

    def test_project_relative_path_out_of_the_project_is_full(self):
        # Arrange
        include = "$(MSBuildProjectDirectory)/../../docs/rules.md"

        # Act
        result = self.classify_with(include, "docs/rules.md")

        # Assert
        self.assertEqual(("full", ["docs/rules.md"]), (result["scope"], result["nonDocumentation"]))

    def test_project_relative_glob_out_of_the_project_is_full(self):
        # Arrange
        include = "$(MSBuildProjectDirectory)\\..\\..\\docs\\**\\*.md"

        # Act
        result = self.classify_with(include, "docs/analyzers/rules.md")

        # Assert
        self.assertEqual("full", result["scope"])


class ImportedRelativePathTests(unittest.TestCase):
    def setUp(self):
        self.repository = Repository()
        self.addCleanup(self.repository.close)

    def classify_with(self, include):
        self.repository.write("build/Inputs.props",
                              f'<Project><ItemGroup><AdditionalFiles Include="{include}" /></ItemGroup></Project>')
        self.repository.base = self.repository.commit()
        self.repository.write("docs/guide.md", "Changed guide")
        return self.repository.classify()

    def test_imported_relative_path_resolves_from_the_project(self):
        # Arrange
        include = "../../docs/guide.md"

        # Act
        result = self.classify_with(include)

        # Assert
        self.assertEqual(("full", ["docs/guide.md"]), (result["scope"], result["nonDocumentation"]))

    def test_property_inside_an_imported_path_is_full(self):
        # Arrange
        include = "../../$(DocDir)/guide.md"

        # Act
        result = self.classify_with(include)

        # Assert
        self.assertEqual(("full", ["docs/guide.md"]), (result["scope"], result["nonDocumentation"]))

    def test_property_inside_a_project_path_is_full(self):
        # Arrange
        self.repository.write("src/Core/Core.csproj", '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup>'
                              '<AdditionalFiles Include="../../$(DocDir)/guide.md" /></ItemGroup></Project>')
        self.repository.base = self.repository.commit()
        self.repository.write("docs/guide.md", "Changed guide")

        # Act
        result = self.repository.classify()

        # Assert
        self.assertEqual("full", result["scope"])

    def test_this_file_directory_resolves_from_the_imported_file(self):
        # Arrange
        include = "$(MSBuildThisFileDirectory)../docs/guide.md"

        # Act
        result = self.classify_with(include)

        # Assert
        self.assertEqual("full", result["scope"])


class MSBuildReachTests(unittest.TestCase):
    def setUp(self):
        self.repository = Repository()
        self.addCleanup(self.repository.close)

    def change_after(self, files, changed):
        for path, text in files.items():
            self.repository.write(path, text)

        self.repository.base = self.repository.commit()
        self.repository.write(changed, "changed")
        return self.repository.classify()

    def test_literal_path_through_a_symlink_is_full(self):
        # Arrange
        (self.repository.root / "linked-docs").symlink_to("docs", target_is_directory=True)
        files = {"build/Inputs.props": '<Project><ItemGroup><AdditionalFiles Include="../../linked-docs/guide.md" />'
                                       "</ItemGroup></Project>"}

        # Act
        result = self.change_after(files, "docs/guide.md")

        # Assert
        self.assertEqual("full", result["scope"])

    def test_glob_through_a_symlink_is_full(self):
        # Arrange
        (self.repository.root / "linked-docs").symlink_to("docs", target_is_directory=True)
        files = {"build/Inputs.props": '<Project><ItemGroup><AdditionalFiles Include="../../linked-docs/**/*.md" />'
                                       "</ItemGroup></Project>"}

        # Act
        result = self.change_after(files, "docs/reference/guide.md")

        # Assert
        self.assertEqual("full", result["scope"])

    def test_glob_after_a_property_is_full(self):
        # Arrange
        files = {"build/Inputs.props": '<Project><ItemGroup><AdditionalFiles Include="../../$(DocDir)/**/*.md" />'
                                       "</ItemGroup></Project>"}

        # Act
        result = self.change_after(files, "docs/reference/guide.md")

        # Assert
        self.assertEqual("full", result["scope"])

    def test_shared_project_items_are_read(self):
        # Arrange
        # Shared projects name their files from their own folder, as Visual Studio writes them.
        files = {"shared/Shared.projitems": "<Project><ItemGroup>"
                                            '<EmbeddedResource Include="$(MSBuildThisFileDirectory)../docs/guide.md" />'
                                            "</ItemGroup></Project>"}

        # Act
        result = self.change_after(files, "docs/guide.md")

        # Assert
        self.assertEqual("full", result["scope"])

    def test_file_named_by_a_property_is_full(self):
        # Arrange
        files = {"src/Directory.Build.props": "<Project><PropertyGroup>"
                                              "<CodeAnalysisRuleSet>../../docs/rules.md</CodeAnalysisRuleSet>"
                                              "</PropertyGroup></Project>",
                 "docs/rules.md": "rules"}

        # Act
        result = self.change_after(files, "docs/rules.md")

        # Assert
        self.assertEqual("full", result["scope"])

    def test_analyzer_configuration_is_never_documentation(self):
        # Arrange
        files = {"docs/analyzers.globalconfig": "is_global = true"}

        # Act
        result = self.change_after(files, "docs/analyzers.globalconfig")

        # Assert
        self.assertEqual("full", result["scope"])

    def test_unreferenced_documentation_stays_docs(self):
        # Arrange
        files = {"build/Inputs.props": '<Project><ItemGroup><AdditionalFiles Include="../../assets/rules/*.txt" />'
                                       "</ItemGroup></Project>"}

        # Act
        result = self.change_after(files, "docs/reference/guide.md")

        # Assert
        self.assertEqual("docs", result["scope"])


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.repository = Repository()
        self.addCleanup(self.repository.close)

    def change_after(self, files, changed="docs/guide.md"):
        for path, text in files.items():
            self.repository.write(path, text)

        self.repository.base = self.repository.commit()
        self.repository.write(changed, "changed")
        return self.repository.classify()

    def test_escaped_file_name_is_full(self):
        # Arrange
        files = {"build/Inputs.props": '<Project><ItemGroup><AdditionalFiles Include="$(DocRoot)guide%3Bv2.md" />'
                                       "</ItemGroup></Project>", "docs/guide;v2.md": "guide"}

        # Act
        result = self.change_after(files, "docs/guide;v2.md")

        # Assert
        self.assertEqual("full", result["scope"])

    def test_property_file_name_is_full(self):
        # Arrange
        files = {"build/Inputs.props": "<Project><PropertyGroup><DocName>custom-name.md</DocName></PropertyGroup>"
                                       '<ItemGroup><AdditionalFiles Include="../../docs/$(DocName)" /></ItemGroup>'
                                       "</Project>", "docs/custom-name.md": "custom"}

        # Act
        result = self.change_after(files, "docs/custom-name.md")

        # Assert
        self.assertEqual("full", result["scope"])

    def test_property_stem_is_full(self):
        # Arrange
        files = {"build/Inputs.props": "<Project><PropertyGroup><DocStem>custom-name</DocStem></PropertyGroup>"
                                       '<ItemGroup><AdditionalFiles Include="$(RepositoryDocs)$(DocStem).md" />'
                                       "</ItemGroup></Project>", "notes/custom-name.md": "custom"}
        self.repository.write(".github/change-scope.json", json.dumps(
            {"schemaVersion": 1, "documentation": ["docs/**", "notes/**", "*.md", "**/*.md"]}))

        # Act
        result = self.change_after(files, "notes/custom-name.md")

        # Assert
        self.assertEqual("full", result["scope"])

    def test_property_the_repository_never_sets_names_no_tracked_file(self):
        # Arrange
        files = {"Directory.Build.targets": "<Project><ItemGroup>"
                                            '<AllPublishItemsFullPathWithTargetPath Update="$(ProjectDepsFilePath)" />'
                                            "</ItemGroup></Project>"}

        # Act
        result = self.change_after(files, "docs/reference/guide.md")

        # Assert
        self.assertEqual("docs", result["scope"])

    def test_property_a_script_sets_names_any_file(self):
        # Arrange
        files = {"build/Inputs.props": '<Project><ItemGroup><AdditionalFiles Include="$(DocName)" />'
                                       "</ItemGroup></Project>",
                 "eng/build.sh": "dotnet build -p:DocName=reference.txt"}

        # Act
        result = self.change_after(files, "docs/reference/guide.md")

        # Assert
        self.assertEqual("full", result["scope"])

    def test_imported_project_file_resolves_from_the_importing_project(self):
        # Arrange
        files = {"build/Shared.proj": '<Project><ItemGroup><AdditionalFiles Include="../../manuals/x.txt" />'
                                      "</ItemGroup></Project>",
                 "build/Imported.csproj": '<Project><ItemGroup><AdditionalFiles Include="../../manuals/y.txt" />'
                                          "</ItemGroup></Project>"}
        for path, text in files.items():
            self.repository.write(path, text)

        self.repository.commit()

        # Act
        inputs = scope.build_inputs(self.repository.root)

        # Assert
        self.assertEqual({"manuals/x.txt", "manuals/y.txt"}, {path for path in inputs["paths"]
                                                              if path.startswith("manuals/")})

    def test_separator_after_this_file_directory_stays_relative(self):
        # Arrange
        files = {"build/Inputs.props": "<Project><ItemGroup>"
                                       '<AdditionalFiles Include="$(MSBuildThisFileDirectory)/../manuals/z.txt" />'
                                       "</ItemGroup></Project>"}
        for path, text in files.items():
            self.repository.write(path, text)

        self.repository.commit()

        # Act
        inputs = scope.build_inputs(self.repository.root)

        # Assert
        self.assertIn("manuals/z.txt", inputs["paths"])

    def test_task_reading_a_documentation_file_is_full(self):
        # Arrange
        files = {"build/Inputs.targets": '<Project><Target Name="Notes"><ReadLinesFromFile '
                                         'File="$(MSBuildThisFileDirectory)../docs/guide.md" /></Target></Project>'}

        # Act
        result = self.change_after(files)

        # Assert
        self.assertEqual("full", result["scope"])

    def test_test_opening_the_documentation_directory_is_full(self):
        # Arrange
        files = {"tests/Docs/ReadmeTests.cs": 'var text = File.ReadAllText(Path.Combine(root, "docs", "x.md"));'}

        # Act
        result = self.change_after(files)

        # Assert
        self.assertEqual(("full", ["tests/Docs/ReadmeTests.cs"]),
                         (result["scope"], result["references"]["docs/guide.md"]))

    def test_links_urls_and_prose_do_not_reference(self):
        # Arrange
        files = {"samples/Notes.txt": "See https://example.invalid/blob/main/docs/guide.md for the docs guide.",
                 "notes/index.md": "[Guide](../docs/guide.md)",
                 "tools/build.cs": "// Generates the API docs for every project."}

        # Act
        result = self.change_after(files)

        # Assert
        self.assertEqual("docs", result["scope"])


class ContractGapTests(unittest.TestCase):
    def setUp(self):
        self.repository = Repository()
        self.addCleanup(self.repository.close)

    def change_after(self, files, changed="docs/guide.md"):
        for path, text in files.items():
            self.repository.write(path, text)

        self.repository.base = self.repository.commit()
        self.repository.write(changed, "changed")
        return self.repository.classify()

    def test_script_override_of_a_project_default_is_full(self):
        # Arrange
        files = {"build/Inputs.props": "<Project><PropertyGroup><DocInput>default.txt</DocInput></PropertyGroup>"
                                       '<ItemGroup><AdditionalFiles Include="$(DocInput)" /></ItemGroup></Project>',
                 "eng/build.sh": 'dotnet build -p:DocInput="$1"'}

        # Act
        result = self.change_after(files, "docs/reference/guide.md")

        # Assert
        self.assertEqual("full", result["scope"])

    def test_protected_file_under_a_documentation_path_references(self):
        # Arrange
        files = {"docs/check.py": 'text = open(root / "docs" / "guide.md").read()'}

        # Act
        result = self.change_after(files)

        # Assert
        self.assertEqual(("full", ["docs/check.py"]), (result["scope"], result["references"].get("docs/guide.md")))

    def test_project_directory_has_no_trailing_separator(self):
        # Arrange
        self.repository.write("src/Directory.Build.props", "<Project><ItemGroup>"
                              '<None Include="$(MSBuildProjectDirectory)Notice.md" /></ItemGroup></Project>')
        self.repository.commit()

        # Act
        inputs = scope.build_inputs(self.repository.root)

        # Assert
        self.assertEqual(({"src/CoreNotice.md"}, set()),
                         ({path for path in inputs["paths"] if "Notice" in path},
                          {path for path in inputs["paths"] if path == "src/Core/Notice.md"}))

    def test_symbolic_link_out_of_the_repository_is_full(self):
        # Arrange
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        (Path(outside.name) / "a.md").write_text("a", encoding="utf-8")
        (Path(outside.name) / "b.md").write_text("b", encoding="utf-8")
        link = self.repository.root / "docs" / "external.md"
        link.symlink_to(Path(outside.name) / "a.md")
        self.repository.base = self.repository.commit()
        link.unlink()
        link.symlink_to(Path(outside.name) / "b.md")

        # Act
        result = self.repository.classify()

        # Assert
        self.assertEqual(("full", ["docs/external.md"]), (result["scope"], result["nonDocumentation"]))

    def test_removed_symbolic_link_is_full(self):
        # Arrange
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        target = Path(outside.name) / "target.md"
        target.write_text("target", encoding="utf-8")
        link = self.repository.root / "docs" / "external.md"
        link.symlink_to(target)
        self.repository.base = self.repository.commit()
        link.unlink()

        # Act
        result = self.repository.classify()

        # Assert
        self.assertEqual(("full", ["docs/external.md"]), (result["scope"], result["nonDocumentation"]))

    def test_task_input_directory_can_also_contain_a_project(self):
        # Arrange
        self.repository.write("src/Core/Core.csproj", '<Project><PropertyGroup>'
                              '<InputRoot>$(MSBuildProjectDirectory)/..</InputRoot>'
                              '</PropertyGroup><Target Name="Check">'
                              '<Exec Command="python3 scan.py $(InputRoot)" />'
                              '</Target></Project>')
        self.repository.write("src/manual/guide.md", "Guide")
        self.repository.base = self.repository.commit()
        self.repository.write("src/manual/guide.md", "Changed guide")

        # Act
        result = self.repository.classify()

        # Assert
        self.assertEqual(("full", ["src/manual/guide.md"]), (result["scope"], result["nonDocumentation"]))


class GlobRegexTests(unittest.TestCase):
    def test_translation_matches_full_match(self):
        # Arrange
        patterns = ["docs/**", "**/*.md", "*.md", "docs/**/*.md", "src/Core/*.cs", "a?c/**", "docs/**/x/*.txt"]
        paths = ["docs", "docs/a.md", "docs/a/b.md", "README.md", "src/README.md", "src/Core/A.cs", "src/Core/x/A.cs",
                 "abc/d", "docs/x/y.txt", "docs/q/x/y.txt", "docs/x/z/y.txt"]

        # Act
        mismatches = [(pattern, path) for pattern in patterns for path in paths
                      if bool(re.fullmatch(scope.glob_regex(pattern), path))
                      != PurePosixPath(path).full_match(pattern)]

        # Assert
        self.assertEqual([], mismatches)


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
