import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from tools.release_lines import policy

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "release-policy.json"
COMMIT = "a" * 40


def example():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


class ValidatePolicyTests(unittest.TestCase):
    def test_example_policy_is_valid(self):
        # Arrange
        value = example()

        # Act
        validated = policy.validate_policy(value, repository="doka-labs/Example")

        # Assert
        self.assertEqual(value, validated)

    def test_other_repository_fails(self):
        # Arrange
        value = example()

        # Act
        with self.assertRaises(policy.PolicyError) as raised:
            policy.validate_policy(value, repository="doka-labs/Other")

        # Assert
        self.assertIn("not to doka-labs/Other", str(raised.exception))

    def test_unknown_field_fails(self):
        # Arrange
        value = example()
        value["lines"][0]["workflowPath"] = ".github/workflows/release-candidate.yml"

        # Act
        with self.assertRaises(policy.PolicyError) as raised:
            policy.validate_policy(value)

        # Assert
        self.assertIn("exactly these fields", str(raised.exception))

    def test_maintenance_ref_must_match_major(self):
        # Arrange
        value = example()
        value["lines"][1]["sourceRef"] = "refs/heads/maintenance/9.x"

        # Act
        with self.assertRaises(policy.PolicyError) as raised:
            policy.validate_policy(value)

        # Assert
        self.assertIn("refs/heads/maintenance/10.x", str(raised.exception))

    def test_two_active_lines_for_one_major_fail(self):
        # Arrange
        value = example()
        value["lines"][1]["packageMajor"] = 11
        value["lines"][1]["sourceRef"] = "refs/heads/maintenance/11.x"

        # Act
        with self.assertRaises(policy.PolicyError) as raised:
            policy.validate_policy(value)

        # Assert
        self.assertIn("package major 11", str(raised.exception))

    def test_policy_without_main_fails(self):
        # Arrange
        value = example()
        del value["lines"][0]

        # Act
        with self.assertRaises(policy.PolicyError) as raised:
            policy.validate_policy(value)

        # Assert
        self.assertIn("main line", str(raised.exception))

    def test_stable_line_with_preview_upstream_fails(self):
        # Arrange
        value = example()
        value["lines"][0]["upstreamStage"] = "rc"

        # Act
        with self.assertRaises(policy.PolicyError) as raised:
            policy.validate_policy(value)

        # Assert
        self.assertIn("GA upstream", str(raised.exception))

    def test_preview_line_requires_prerelease_channel(self):
        # Arrange
        value = example()
        value["lines"].append({"sourceRef": "refs/heads/preview/12.x", "role": "preview", "packageMajor": 12,
                               "channel": "stable-and-prerelease", "upstreamStage": "rc", "state": "prepared"})

        # Act
        with self.assertRaises(policy.PolicyError) as raised:
            policy.validate_policy(value)

        # Assert
        self.assertIn("prerelease-only", str(raised.exception))

    def test_boolean_is_not_a_schema_version(self):
        # Arrange
        value = example()
        value["schemaVersion"] = True

        # Act
        with self.assertRaises(policy.PolicyError) as raised:
            policy.validate_policy(value)

        # Assert
        self.assertIn("schemaVersion 1", str(raised.exception))

    def test_duplicate_workload_fails(self):
        # Arrange
        value = example()
        value["workloads"].append(copy.deepcopy(value["workloads"][0]))

        # Act
        with self.assertRaises(policy.PolicyError) as raised:
            policy.validate_policy(value)

        # Assert
        self.assertIn("Duplicate workload id", str(raised.exception))

    def test_lockfile_outside_repository_fails(self):
        # Arrange
        value = example()
        value["toolchain"]["lockfiles"] = ["../other/packages.lock.json"]

        # Act
        with self.assertRaises(policy.PolicyError) as raised:
            policy.validate_policy(value)

        # Assert
        self.assertIn("relative to the repository root", str(raised.exception))

    def test_unknown_gate_kind_fails(self):
        # Arrange
        value = example()
        value["qualification"]["gates"][0]["kind"] = "manual"

        # Act
        with self.assertRaises(policy.PolicyError) as raised:
            policy.validate_policy(value)

        # Assert
        self.assertIn("gate kind", str(raised.exception))


class ParsePolicyTests(unittest.TestCase):
    def test_duplicate_property_fails(self):
        # Arrange
        data = EXAMPLE.read_text(encoding="utf-8").replace('"revision": "2026-11-22.1",',
                                                           '"revision": "a", "revision": "b",')

        # Act
        with self.assertRaises(policy.PolicyError) as raised:
            policy.parse_policy(data)

        # Assert
        self.assertIn("duplicate property: revision", str(raised.exception))

    def test_oversized_policy_fails(self):
        # Arrange
        data = b" " * (policy.MAX_POLICY_BYTES + 1)

        # Act
        with self.assertRaises(policy.PolicyError) as raised:
            policy.parse_policy(data)

        # Assert
        self.assertIn("1 MiB", str(raised.exception))

    def test_symbolic_link_policy_fails(self):
        # Arrange
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github").mkdir()
            os.symlink(EXAMPLE, root / policy.POLICY_PATH)

            # Act
            with self.assertRaises(policy.PolicyError) as raised:
                policy.load_policy(root)

        # Assert
        self.assertIn("regular file", str(raised.exception))

    def test_digest_ignores_layout_and_key_order(self):
        # Arrange
        value = example()
        reordered = json.loads(json.dumps(value, sort_keys=True, indent=4))

        # Act
        digests = {policy.policy_digest(value), policy.policy_digest(reordered)}

        # Assert
        self.assertEqual(1, len(digests))

    def test_digest_covers_qualification(self):
        # Arrange
        value = example()
        changed = example()
        changed["qualification"]["testResults"]["skips"] = []

        # Act
        digests = {policy.policy_digest(value), policy.policy_digest(changed)}

        # Assert
        self.assertEqual(2, len(digests))


class VersionTests(unittest.TestCase):
    def test_noncanonical_versions_fail(self):
        # Arrange
        values = ("1.0", "01.0.0", "1.0.0+build", "1.0.0-RC.1", "1.0.0-rc.01", "1.0.0--rc", "1.0.0-rc-")

        for value in values:
            with self.subTest(value=value):
                # Act
                with self.assertRaises(policy.PolicyError) as raised:
                    policy.parse_version(value)

                # Assert
                self.assertIn("ersion", str(raised.exception))

    def test_semver_precedence(self):
        # Arrange
        ordered = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta.2", "1.0.0-beta.11",
                   "1.0.0-rc.1", "1.0.0", "1.0.1", "1.1.0", "2.0.0"]

        # Act
        comparisons = [policy.compare_versions(left, right) for left, right in zip(ordered, ordered[1:])]

        # Assert
        self.assertEqual([-1] * (len(ordered) - 1), comparisons)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.policy = example()
        self.policy["lines"][1]["state"] = "prepared"

    def test_prepared_line_qualifies_but_does_not_publish(self):
        # Arrange
        ref = "refs/heads/maintenance/10.x"

        # Act
        selected = policy.select_line(self.policy, ref, "10.4.1", purpose="qualification")
        with self.assertRaises(policy.PolicyError) as raised:
            policy.select_line(self.policy, ref, "10.4.1", purpose="publication")

        # Assert
        self.assertEqual(ref, selected["sourceRef"])
        self.assertIn("eligible for publication", str(raised.exception))

    def test_version_of_other_major_fails(self):
        # Arrange
        ref = "refs/heads/main"

        # Act
        with self.assertRaises(policy.PolicyError) as raised:
            policy.select_line(self.policy, ref, "10.0.0", purpose="publication")

        # Assert
        self.assertIn("package major 11", str(raised.exception))

    def test_maintenance_targets_only_active_lines(self):
        # Arrange
        value = self.policy

        # Act
        refs = [line["sourceRef"] for line in policy.lines_for(value, "maintenance")]

        # Assert
        self.assertEqual(["refs/heads/main"], refs)

    def test_pr_base_requires_registered_line(self):
        # Arrange
        base = "refs/heads/feature/x"

        # Act
        with self.assertRaises(policy.PolicyError) as raised:
            policy.check_pr_base(self.policy, base)

        # Assert
        self.assertIn("not a registered line", str(raised.exception))

    def test_missing_policy_allows_only_main_as_pr_base(self):
        # Arrange
        bases = ("refs/heads/main", "refs/heads/maintenance/10.x")

        # Act
        accepted = policy.check_pr_base(None, bases[0])
        with self.assertRaises(policy.PolicyError) as raised:
            policy.check_pr_base(None, bases[1])

        # Assert
        self.assertIsNone(accepted)
        self.assertIn("only main", str(raised.exception))


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.policy = example()

    def test_frozen_snapshot_validates(self):
        # Arrange
        ref = "refs/heads/maintenance/10.x"

        # Act
        snapshot = policy.freeze_line(self.policy, ref, "10.4.1", policy_commit=COMMIT, qualification_only=False)

        # Assert
        self.assertEqual({"sourceRef": ref, "channel": "stable", "stateAtQualification": "active",
                          "qualificationOnly": False, "policyDigest": policy.policy_digest(self.policy)},
                         {key: snapshot[key] for key in ("sourceRef", "channel", "stateAtQualification",
                                                         "qualificationOnly", "policyDigest")})

    def test_prepared_line_cannot_freeze_for_publication(self):
        # Arrange
        self.policy["lines"][1]["state"] = "prepared"

        # Act
        with self.assertRaises(policy.PolicyError) as raised:
            policy.freeze_line(self.policy, "refs/heads/maintenance/10.x", "10.4.1", policy_commit=COMMIT,
                               qualification_only=False)

        # Assert
        self.assertIn("eligible for publication", str(raised.exception))

    def test_prepared_snapshot_claiming_publication_fails(self):
        # Arrange
        self.policy["lines"][1]["state"] = "prepared"
        snapshot = policy.freeze_line(self.policy, "refs/heads/maintenance/10.x", "10.4.1", policy_commit=COMMIT,
                                      qualification_only=True)
        snapshot["qualificationOnly"] = False

        # Act
        with self.assertRaises(policy.PolicyError) as raised:
            policy.validate_snapshot(snapshot)

        # Assert
        self.assertIn("qualification-only", str(raised.exception))

    def test_short_commit_fails(self):
        # Arrange
        ref = "refs/heads/main"

        # Act
        with self.assertRaises(policy.PolicyError) as raised:
            policy.freeze_line(self.policy, ref, "11.0.0", policy_commit="abc123", qualification_only=False)

        # Assert
        self.assertIn("policyCommit", str(raised.exception))


class CoherentGroupTests(unittest.TestCase):
    def test_group_member_outside_framework_packages_fails(self):
        # Arrange
        value = example()
        value["toolchain"]["coherentGroups"] = [["Microsoft.EntityFrameworkCore", "Pomelo.EntityFrameworkCore.MySql"]]

        # Act
        with self.assertRaises(policy.PolicyError) as raised:
            policy.validate_policy(value)

        # Assert
        self.assertIn("Pomelo.EntityFrameworkCore.MySql", str(raised.exception))

    def test_group_inside_framework_patterns_passes(self):
        # Arrange
        value = example()
        value["toolchain"]["coherentGroups"] = [["microsoft.entityframeworkcore.relational",
                                                 "Microsoft.EntityFrameworkCore.Design"]]

        # Act
        result = policy.validate_policy(value)

        # Assert
        self.assertEqual(value["toolchain"]["coherentGroups"], result["toolchain"]["coherentGroups"])


class CandidateGateWorkflowTests(unittest.TestCase):
    def test_candidate_gate_outside_the_release_workflow_fails(self):
        # Arrange
        value = example()
        value["qualification"]["gates"].append({"name": "release / Build", "kind": "candidate-produced",
                                                "workflow": ".github/workflows/ci.yml"})

        # Act
        with self.assertRaises(policy.PolicyError) as raised:
            policy.validate_policy(value)

        # Assert
        self.assertIn("release workflowPath: release / Build", str(raised.exception))

    def test_candidate_gate_of_the_release_workflow_passes(self):
        # Arrange
        value = example()
        value["qualification"]["gates"].append({"name": "release / Build", "kind": "candidate-produced",
                                                "workflow": value["release"]["workflowPath"]})

        # Act
        result = policy.validate_policy(value)

        # Assert
        self.assertEqual(2, len(result["qualification"]["gates"]))


if __name__ == "__main__":
    unittest.main()
