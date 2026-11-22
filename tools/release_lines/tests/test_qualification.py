import tempfile
import unittest
from pathlib import Path

from tools.release_lines import attestation, qualification
from tools.release_lines.github import GitHubError

NAMESPACE = "http://microsoft.com/schemas/VisualStudio/TeamTest/2010"
TREE = "c" * 40
CI = ".github/workflows/ci.yml"
OTHER = ".github/workflows/other.yml"
PROJECTS = {"Doka.Example.Tests": "tests/Doka.Example.Tests/Doka.Example.Tests.csproj",
            "Doka.Example.FunctionalTests": "tests/Doka.Example.FunctionalTests/Doka.Example.FunctionalTests.csproj"}


def trx(results, outcome=None, counters=None):
    definitions, entries = [], []
    for index, (assembly, outcome, reason) in enumerate(results):
        test_id = f"00000000-0000-0000-0000-{index:012d}"
        definitions.append(f'<UnitTest name="T{index}" id="{test_id}"><TestMethod '
                           f'codeBase="/w/artifacts/bin/{assembly}/release/{assembly}.dll" className="C" '
                           f'name="T{index}" /></UnitTest>')
        output = (f"<Output><ErrorInfo><Message>{reason}</Message></ErrorInfo></Output>" if reason is not None else "")
        entries.append(f'<UnitTestResult testId="{test_id}" testName="T{index}" outcome="{outcome}">{output}'
                       "</UnitTestResult>")

    failed = sum(item[1] == "Failed" for item in results)
    values = {"total": len(results), "failed": failed, "error": 0, "timeout": 0, "aborted": 0, **(counters or {})}
    summary = (f'<ResultSummary outcome="{outcome or ("Failed" if failed else "Completed")}"><Counters '
               + " ".join(f'{name}="{value}"' for name, value in values.items()) + " /></ResultSummary>")
    return (f'<?xml version="1.0" encoding="utf-8"?><TestRun xmlns="{NAMESPACE}"><Results>{"".join(entries)}'
            f'</Results><TestDefinitions>{"".join(definitions)}</TestDefinitions>{summary}</TestRun>')


class TestResultTests(unittest.TestCase):
    summary = {}

    def check(self, files, skips=frozenset()):
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for name, results in files.items():
                path = Path(directory) / name
                path.write_text(trx(results, **self.summary), encoding="utf-8")
                paths.append(path)

            return qualification.check_tests(paths, PROJECTS, set(skips))

    def failure(self, files, skips=frozenset()):
        with self.assertRaises(qualification.QualificationError) as raised:
            self.check(files, skips)

        return str(raised.exception)

    def test_complete_passing_results_pass(self):
        # Arrange
        files = {"unit.trx": [("Doka.Example.Tests", "Passed", None)] * 2,
                 "functional.trx": [("Doka.Example.FunctionalTests", "Passed", None)]}

        # Act
        summary = self.check(files)

        # Assert
        self.assertEqual([("Doka.Example.FunctionalTests", 1, 0), ("Doka.Example.Tests", 2, 0)],
                         [(item["assembly"], item["passed"], item["skipped"]) for item in summary])

    def test_failed_test_fails(self):
        # Arrange
        files = {"unit.trx": [("Doka.Example.Tests", "Failed", None), ("Doka.Example.FunctionalTests", "Passed", None)]}

        # Act
        message = self.failure(files)

        # Assert
        self.assertIn("reports a failed, errored, timed out or aborted run", message)

    def test_failed_result_under_a_clean_summary_fails(self):
        # Arrange
        files = {"unit.trx": [("Doka.Example.Tests", "Failed", None), ("Doka.Example.FunctionalTests", "Passed", None)]}
        self.summary = {"outcome": "Completed", "counters": {"failed": 0}}

        # Act
        message = self.failure(files)

        # Assert
        self.assertIn("ended with Failed", message)

    def test_aborted_run_with_passing_results_fails(self):
        # Arrange
        files = {"unit.trx": [("Doka.Example.Tests", "Passed", None), ("Doka.Example.FunctionalTests", "Passed", None)]}
        self.summary = {"outcome": "Aborted", "counters": {"error": 1}}

        # Act
        message = self.failure(files)

        # Assert
        self.assertIn("reports a failed, errored, timed out or aborted run", message)

    def test_summary_with_another_total_fails(self):
        # Arrange
        files = {"unit.trx": [("Doka.Example.Tests", "Passed", None), ("Doka.Example.FunctionalTests", "Passed", None)]}
        self.summary = {"outcome": None, "counters": {"total": 3}}

        # Act
        message = self.failure(files)

        # Assert
        self.assertIn("summarizes another number of results", message)

    def test_project_without_results_fails(self):
        # Arrange
        files = {"unit.trx": [("Doka.Example.Tests", "Passed", None)]}

        # Act
        message = self.failure(files)

        # Assert
        self.assertIn("without results: Doka.Example.FunctionalTests", message)

    def test_results_of_unknown_assembly_fail(self):
        # Arrange
        files = {"unit.trx": [("Doka.Example.Tests", "Passed", None), ("Doka.Example.FunctionalTests", "Passed", None),
                              ("Doka.Other.Tests", "Passed", None)]}

        # Act
        message = self.failure(files)

        # Assert
        self.assertIn("Doka.Other.Tests, which is not a test project", message)

    def test_same_test_in_two_files_fails(self):
        # Arrange
        results = [("Doka.Example.Tests", "Passed", None), ("Doka.Example.FunctionalTests", "Passed", None)]
        files = {"a.trx": results, "b.trx": results}

        # Act
        message = self.failure(files)

        # Assert
        self.assertIn("appears in a.trx and b.trx", message)

    def test_skip_with_reason_in_named_project_passes(self):
        # Arrange
        files = {"unit.trx": [("Doka.Example.Tests", "Passed", None),
                              ("Doka.Example.FunctionalTests", "NotExecuted", "[spec-engine-limit:X] reason")]}

        # Act
        summary = self.check(files, {"Doka.Example.FunctionalTests"})

        # Assert
        self.assertEqual(1, summary[0]["skipped"])

    def test_skip_outside_named_projects_fails(self):
        # Arrange
        files = {"unit.trx": [("Doka.Example.Tests", "NotExecuted", "reason"),
                              ("Doka.Example.FunctionalTests", "Passed", None)]}

        # Act
        message = self.failure(files, {"Doka.Example.FunctionalTests"})

        # Assert
        self.assertIn("accepts no skips there", message)

    def test_skip_without_reason_fails(self):
        # Arrange
        files = {"unit.trx": [("Doka.Example.Tests", "Passed", None),
                              ("Doka.Example.FunctionalTests", "NotExecuted", "  ")]}

        # Act
        message = self.failure(files, {"Doka.Example.FunctionalTests"})

        # Assert
        self.assertIn("without a reason", message)

    def test_trx_digests_are_recorded(self):
        # Arrange
        files = {"unit.trx": [("Doka.Example.Tests", "Passed", None), ("Doka.Example.FunctionalTests", "Passed", None)]}

        # Act
        summary = self.check(files)

        # Assert
        self.assertEqual(64, len(summary[0]["trx"][0]["sha256"]))

    def test_no_results_fail(self):
        # Arrange
        files = {}

        # Act
        message = self.failure(files)

        # Assert
        self.assertIn("No TRX", message)


class PackageValidationTests(unittest.TestCase):
    def check(self, evaluated, ids):
        projects = [Path(f"/repo/src/{name}/{name}.csproj") for name in evaluated]
        original = (qualification.source_projects, qualification.evaluate)
        qualification.source_projects = lambda root: projects
        qualification.evaluate = lambda root, project, names: evaluated[project.stem]
        try:
            return qualification.require_package_validation(Path("/repo"), ids)
        finally:
            qualification.source_projects, qualification.evaluate = original

    def test_validated_packages_pass(self):
        # Arrange
        evaluated = {"Core": {"PackageId": "Doka.Core", "IsPackable": "true", "EnablePackageValidation": "true"},
                     "Tests": {"PackageId": "Tests", "IsPackable": "false", "EnablePackageValidation": ""}}

        # Act
        result = self.check(evaluated, ["Doka.Core"])

        # Assert
        self.assertEqual([{"package": "Doka.Core", "project": "src/Core/Core.csproj"}], result)

    def test_package_without_validation_fails(self):
        # Arrange
        evaluated = {"Core": {"PackageId": "Doka.Core", "IsPackable": "true", "EnablePackageValidation": "false"}}

        # Act
        with self.assertRaises(qualification.QualificationError) as raised:
            self.check(evaluated, ["Doka.Core"])

        # Assert
        self.assertIn("without EnablePackageValidation", str(raised.exception))

    def test_missing_package_fails(self):
        # Arrange
        evaluated = {"Core": {"PackageId": "Doka.Core", "IsPackable": "true", "EnablePackageValidation": "true"}}

        # Act
        with self.assertRaises(qualification.QualificationError) as raised:
            self.check(evaluated, ["Doka.Core", "Doka.Other"])

        # Assert
        self.assertIn("Doka.Other", str(raised.exception))


class GateJobTests(unittest.TestCase):
    def test_gate_with_successful_matrix_rows_passes(self):
        # Arrange
        jobs = [{"name": "ci / tests (mysql)", "status": "completed", "conclusion": "success"},
                {"name": "ci / tests (mariadb)", "status": "completed", "conclusion": "success"}]

        # Act
        gates = qualification.require_gate_jobs(jobs, ["ci / tests"])

        # Assert
        self.assertEqual(["ci / tests (mariadb)", "ci / tests (mysql)"], gates[0]["jobs"])

    def test_skipped_gate_fails(self):
        # Arrange
        jobs = [{"name": "repository-qualification", "status": "completed", "conclusion": "skipped"}]

        # Act
        with self.assertRaises(qualification.QualificationError) as raised:
            qualification.require_gate_jobs(jobs, ["repository-qualification"])

        # Assert
        self.assertIn("missing, skipped or unsuccessful", str(raised.exception))

    def test_missing_gate_fails(self):
        # Arrange
        jobs = [{"name": "other", "status": "completed", "conclusion": "success"}]

        # Act
        with self.assertRaises(qualification.QualificationError) as raised:
            qualification.require_gate_jobs(jobs, ["repository-qualification"])

        # Assert
        self.assertIn("'repository-qualification'", str(raised.exception))


class PrQualificationTests(unittest.TestCase):
    def verify(self, predicates):
        def fake(subject, repository, predicate_type):
            self.assertEqual(attestation.PR_QUALIFICATION, predicate_type)
            return [{"predicate": predicate, "runId": run, "attempt": attempt} for predicate, run, attempt in predicates]

        return fake

    def test_newest_matching_attestation_is_selected(self):
        # Arrange
        base = {"tree": TREE, "repository": "doka-labs/Example", "baseRef": "refs/heads/main", "workflow": CI}
        fake = self.verify([(base, 10, 1), (base, 12, 1), (base, 12, 2), ({**base, "baseRef": "refs/heads/x"}, 99, 1),
                            ({**base, "workflow": OTHER}, 98, 1)])

        # Act
        selected = qualification.verify_pr_qualification(Path("s"), "doka-labs/Example", TREE, "refs/heads/main", CI,
                                                         verify=fake)

        # Assert
        self.assertEqual((12, 2), (selected["runId"], selected["attempt"]))

    def test_attestation_of_other_line_fails(self):
        # Arrange
        fake = self.verify([({"tree": TREE, "repository": "doka-labs/Example", "baseRef": "refs/heads/x",
                              "workflow": CI}, 1, 1)])

        # Act
        with self.assertRaises(qualification.QualificationError) as raised:
            qualification.verify_pr_qualification(Path("s"), "doka-labs/Example", TREE, "refs/heads/main", CI,
                                                  verify=fake)

        # Assert
        self.assertIn("No pull request into refs/heads/main", str(raised.exception))

    def test_attestation_of_other_workflow_fails(self):
        # Arrange
        fake = self.verify([({"tree": TREE, "repository": "doka-labs/Example", "baseRef": "refs/heads/main",
                              "workflow": OTHER}, 1, 1)])

        # Act
        with self.assertRaises(qualification.QualificationError) as raised:
            qualification.verify_pr_qualification(Path("s"), "doka-labs/Example", TREE, "refs/heads/main", CI,
                                                  verify=fake)

        # Assert
        self.assertIn(f"with {CI}", str(raised.exception))


class AttestationTests(unittest.TestCase):
    def test_tree_subject_holds_only_the_tree(self):
        # Arrange
        with tempfile.TemporaryDirectory() as directory:
            # Act
            path = attestation.tree_subject(Path(directory), TREE)
            content = path.read_text(encoding="ascii")

        # Assert
        self.assertEqual((f"git-tree-{TREE}.txt", f"{TREE}\n"), (path.name, content))

    def test_invalid_tree_fails(self):
        # Arrange
        with tempfile.TemporaryDirectory() as directory:
            # Act
            with self.assertRaises(GitHubError) as raised:
                attestation.tree_subject(Path(directory), "abc")

        # Assert
        self.assertIn("40 lowercase", str(raised.exception))

    def test_verify_reads_predicate_and_run_identity(self):
        # Arrange
        output = ('[{"verificationResult": {"statement": {"predicateType": "%s", "predicate": {"tree": "t"}, '
                  '"subject": [{"name": "git-tree-t.txt", "digest": {"sha256": "ab"}}]}, '
                  '"signature": {"certificate": {"runInvocationURI": '
                  '"https://github.com/doka-labs/Example/actions/runs/42/attempts/3", "sourceRepositoryDigest": "d", '
                  '"sourceRepositoryRef": "refs/pull/1/merge"}}}}]' % attestation.PR_QUALIFICATION)

        def run(command, **kwargs):
            self.assertIn("--signer-workflow", command)
            self.assertEqual(attestation.SIGNERS[attestation.PR_QUALIFICATION],
                             command[command.index("--signer-workflow") + 1])
            return type("Completed", (), {"returncode": 0, "stdout": output})()

        # Act
        result = attestation.verify(Path("s"), "doka-labs/Example", attestation.PR_QUALIFICATION, run=run)

        # Assert
        self.assertEqual([(42, 3, {"tree": "t"}, {"git-tree-t.txt": "ab"})],
                         [(item["runId"], item["attempt"], item["predicate"], item["subjects"]) for item in result])

    def test_attestation_of_other_repository_fails(self):
        # Arrange
        output = ('[{"verificationResult": {"statement": {"predicateType": "%s", "predicate": {}}, '
                  '"signature": {"certificate": {"runInvocationURI": '
                  '"https://github.com/other/Repo/actions/runs/42/attempts/1"}}}}]' % attestation.PR_QUALIFICATION)

        def run(command, **kwargs):
            return type("Completed", (), {"returncode": 0, "stdout": output})()

        # Act
        with self.assertRaises(GitHubError) as raised:
            attestation.verify(Path("s"), "doka-labs/Example", attestation.PR_QUALIFICATION, run=run)

        # Assert
        self.assertIn("run identity", str(raised.exception))


class GateBindingTests(unittest.TestCase):
    RELEASE = ".github/workflows/release.yml"

    def qualify(self, gates, attested):
        from unittest import mock

        from tools.release_lines import coordinator

        policy = {"repository": "doka-labs/Example", "release": {"packages": []},
                  "qualification": {"gates": gates, "testResults": {"skips": []}}}
        environment = {"GITHUB_REPOSITORY": "doka-labs/Example", "GITHUB_REF": "refs/heads/main",
                       "GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "5", "GITHUB_RUN_ATTEMPT": "1",
                       "GITHUB_WORKFLOW_REF": f"doka-labs/Example/{self.RELEASE}@refs/heads/main"}

        def verify(subject, repository, predicate_type):
            return [{"predicate": {"tree": TREE, "repository": "doka-labs/Example", "baseRef": "refs/heads/main",
                                   "workflow": workflow, "gates": [{"name": name, "conclusion": "success"}
                                                                   for name in names]},
                     "runId": index + 1, "attempt": 1} for index, (workflow, names) in enumerate(attested)]

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(qualification, "git_tree", return_value=TREE), \
                mock.patch.object(coordinator, "observe_authority", return_value={"policy": policy, "commit": "b" * 40}), \
                mock.patch.object(qualification.policies, "freeze_line", return_value={}), \
                mock.patch.object(qualification, "run_jobs", return_value=[
                    {"name": "release / Build", "status": "completed", "conclusion": "success"}]), \
                mock.patch.object(qualification, "check_tests", return_value=[]), \
                mock.patch.object(qualification, "test_projects", return_value={}), \
                mock.patch.object(qualification, "require_package_validation", return_value=[]):
            return qualification.qualify(None, environment, Path(directory), version="1.2.3", qualification_only=False,
                                         trx_dir=Path(directory), output=Path(directory), verify=verify)

    def test_each_gate_counts_in_the_attestation_of_its_workflow(self):
        # Arrange
        gates = [{"name": "ci / Build", "kind": "protected-check", "workflow": CI},
                 {"name": "other / Check", "kind": "protected-check", "workflow": OTHER}]

        # Act
        result = self.qualify(gates, [(CI, ["ci / Build"]), (OTHER, ["other / Check"])])

        # Assert
        self.assertEqual(2, result["gates"])

    def test_gate_attested_by_another_workflow_fails(self):
        # Arrange
        gates = [{"name": "ci / Build", "kind": "protected-check", "workflow": CI}]

        # Act
        with self.assertRaises(qualification.QualificationError) as raised:
            self.qualify(gates, [(OTHER, ["ci / Build"])])

        # Assert
        self.assertIn(f"with {CI}", str(raised.exception))

    def test_candidate_gate_of_another_workflow_fails(self):
        # Arrange
        gates = [{"name": "release / Build", "kind": "candidate-produced", "workflow": OTHER}]

        # Act
        with self.assertRaises(qualification.QualificationError) as raised:
            self.qualify(gates, [])

        # Assert
        self.assertIn("belong to another workflow", str(raised.exception))

    def test_candidate_gate_of_this_workflow_passes(self):
        # Arrange
        gates = [{"name": "release / Build", "kind": "candidate-produced", "workflow": self.RELEASE}]

        # Act
        result = self.qualify(gates, [])

        # Assert
        self.assertEqual(1, result["gates"])


class EvaluateTests(unittest.TestCase):
    def test_msbuild_runs_in_the_product_checkout(self):
        # Arrange
        from unittest import mock

        root = Path("/product")
        completed = mock.Mock(returncode=0, stdout='{"Properties": {"PackageId": "Doka.Core"}}')

        # Act
        with mock.patch.object(qualification.subprocess, "run", return_value=completed) as run:
            qualification.evaluate(root, root / "src/A/A.csproj", ["PackageId"])

        # Assert
        self.assertEqual(root, run.call_args.kwargs["cwd"])


if __name__ == "__main__":
    unittest.main()
