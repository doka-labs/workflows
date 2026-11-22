import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.release_lines import coordinator, guard
from tools.release_lines.github import GitHubError

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "release-policy.json"
REPOSITORY = "doka-labs/Example"
MAIN = "1" * 40
MAINTENANCE = "2" * 40
RUN_ID = 777


def policy_bytes(mutate=None):
    value = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    if mutate:
        mutate(value)

    return json.dumps(value).encode("ascii")


def contents(raw):
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
    return {"type": "file", "path": ".github/release-policy.json", "encoding": "base64",
            "content": base64.b64encode(raw).decode("ascii"), "sha": blob}


def run(status="completed", conclusion="success", title="maintenance schedule-5-1", **overrides):
    value = {"id": RUN_ID, "event": "workflow_dispatch", "head_sha": MAINTENANCE, "head_branch": "maintenance/10.x",
             "path": ".github/workflows/dependency-audit.yml@refs/heads/maintenance/10.x", "status": status,
             "conclusion": conclusion, "display_title": title}
    value.update(overrides)
    return value


def jobs(*entries):
    return {"total_count": len(entries), "jobs": [
        {"id": index + 1, "name": name, "status": "completed", "conclusion": conclusion}
        for index, (name, conclusion) in enumerate(entries)]}


class FakeApi:
    def __init__(self, routes):
        self.routes = {key: list(value) if isinstance(value, list) else [value] for key, value in routes.items()}
        self.calls = []

    def request(self, route, method="GET", payload=None):
        self.calls.append((method, route, payload))
        responses = self.routes.get((method, route.split("?", 1)[0]))
        if not responses:
            raise AssertionError(f"Unexpected request {method} {route}")

        response = responses.pop(0) if len(responses) > 1 else responses[0]
        if isinstance(response, GitHubError):
            raise response

        return response


def base_routes(raw=None, maintenance_protected=True):
    return {
        ("GET", f"repos/{REPOSITORY}/branches/main"): {"name": "main", "protected": True, "commit": {"sha": MAIN}},
        ("GET", f"repos/{REPOSITORY}/branches/maintenance%2F10.x"): {
            "name": "maintenance/10.x", "protected": maintenance_protected, "commit": {"sha": MAINTENANCE}},
        ("GET", f"repos/{REPOSITORY}/contents/.github/release-policy.json"): contents(raw or policy_bytes()),
        ("POST", f"repos/{REPOSITORY}/actions/workflows/dependency-audit.yml/dispatches"): {
            "workflow_run_id": RUN_ID},
        ("GET", f"repos/{REPOSITORY}/actions/runs/{RUN_ID}/jobs"): jobs(("dependency-audit / Audit", "success")),
    }


def environment(**overrides):
    value = {"GITHUB_EVENT_NAME": "schedule", "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_REF": "refs/heads/main",
             "GITHUB_SHA": MAIN, "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/.github/workflows/schedule.yml@refs/heads/main",
             "GITHUB_RUN_ID": "5", "GITHUB_RUN_ATTEMPT": "1"}
    value.update(overrides)
    return value


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 1.0)


def wait(api, clock=None):
    clock = clock or FakeClock()
    workload = {"workflow": ".github/workflows/dependency-audit.yml", "requiredJobs": ["dependency-audit / Audit"],
                "timeoutMinutes": 60}
    return coordinator.wait_for_run(api, REPOSITORY, RUN_ID, workload, "refs/heads/maintenance/10.x", MAINTENANCE,
                                    "schedule-5-1", record=lambda update: None, clock=clock, sleep=clock.sleep)


class CoordinateTests(unittest.TestCase):
    def coordinate(self, api, env=None):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            try:
                result = coordinator.coordinate(api, env or environment(), "audit", output,
                                                dispatch=lambda *args, **kwargs: coordinator.dispatch_line(
                                                    *args, **kwargs, wait=lambda *a, **k: wait(a[0])))
            finally:
                saved = json.loads(output.read_text(encoding="ascii"))

        return result, saved

    def test_dispatches_maintenance_line_with_exact_identity(self):
        # Arrange
        routes = base_routes()
        routes[("GET", f"repos/{REPOSITORY}/actions/runs/{RUN_ID}")] = run()
        api = FakeApi(routes)

        # Act
        result, saved = self.coordinate(api)

        # Assert
        dispatch = next(call for call in api.calls if call[0] == "POST")
        self.assertEqual({"ref": "maintenance/10.x", "inputs": {"maintenance": True,
                          "schedule-correlation": "schedule-5-1", "schedule-source-sha": MAINTENANCE}}, dispatch[2])
        self.assertEqual("success", result["conclusion"])
        self.assertEqual(result, saved)
        self.assertEqual(["refs/heads/maintenance/10.x"], [child["sourceRef"] for child in result["children"]])

    def test_run_on_stale_main_head_fails(self):
        # Arrange
        api = FakeApi(base_routes())

        # Act
        with self.assertRaises(GitHubError) as raised:
            self.coordinate(api, environment(GITHUB_SHA="9" * 40))

        # Assert
        self.assertEqual("authority_mismatch", raised.exception.code)

    def test_unprotected_line_fails_without_dispatch(self):
        # Arrange
        api = FakeApi(base_routes(maintenance_protected=False))

        # Act
        result, _ = self.coordinate(api)

        # Assert
        self.assertEqual("failure", result["conclusion"])
        self.assertEqual("branch_unprotected", result["children"][0]["failureCode"])
        self.assertFalse(any(call[0] == "POST" for call in api.calls))

    def test_policy_differing_from_blob_fails(self):
        # Arrange
        routes = base_routes()
        tampered = contents(policy_bytes())
        tampered["sha"] = "0" * 40
        routes[("GET", f"repos/{REPOSITORY}/contents/.github/release-policy.json")] = tampered
        api = FakeApi(routes)

        # Act
        with self.assertRaises(GitHubError) as raised:
            self.coordinate(api)

        # Assert
        self.assertEqual("policy_invalid", raised.exception.code)

    def test_inactive_lines_are_not_dispatched(self):
        # Arrange
        raw = policy_bytes(lambda value: value["lines"][1].update(state="inactive"))
        api = FakeApi(base_routes(raw))

        # Act
        result, _ = self.coordinate(api)

        # Assert
        self.assertEqual(([], "success"), (result["children"], result["conclusion"]))


class WaitTests(unittest.TestCase):
    def test_not_yet_visible_run_is_awaited(self):
        # Arrange
        api = FakeApi(base_routes())
        api.routes[("GET", f"repos/{REPOSITORY}/actions/runs/{RUN_ID}")] = [
            GitHubError("x", code="github_http_error", http_status=404), run()]

        # Act
        result = wait(api)

        # Assert
        self.assertEqual("success", result["conclusion"])

    def test_run_invisible_beyond_window_fails(self):
        # Arrange
        api = FakeApi(base_routes())
        api.routes[("GET", f"repos/{REPOSITORY}/actions/runs/{RUN_ID}")] = [
            GitHubError("x", code="github_http_error", http_status=404)]

        # Act
        with self.assertRaises(GitHubError) as raised:
            wait(api)

        # Assert
        self.assertEqual("child_visibility_timeout", raised.exception.code)

    def test_queued_run_with_default_title_is_awaited(self):
        # Arrange
        api = FakeApi(base_routes())
        api.routes[("GET", f"repos/{REPOSITORY}/actions/runs/{RUN_ID}")] = [
            run(status="queued", conclusion=None, title="Dependency Audit"), run()]

        # Act
        result = wait(api)

        # Assert
        self.assertEqual("success", result["conclusion"])

    def test_other_maintenance_title_fails(self):
        # Arrange
        api = FakeApi(base_routes())
        api.routes[("GET", f"repos/{REPOSITORY}/actions/runs/{RUN_ID}")] = [
            run(status="queued", conclusion=None, title="maintenance schedule-4-1")]

        # Act
        with self.assertRaises(GitHubError) as raised:
            wait(api)

        # Assert
        self.assertEqual(("child_identity_mismatch", "display_title"), (raised.exception.code, raised.exception.field))

    def test_completed_without_conclusion_is_awaited(self):
        # Arrange
        api = FakeApi(base_routes())
        api.routes[("GET", f"repos/{REPOSITORY}/actions/runs/{RUN_ID}")] = [
            run(status="in_progress", conclusion=None), run(conclusion=None), run()]

        # Act
        result = wait(api)

        # Assert
        self.assertEqual("success", result["conclusion"])

    def test_completed_without_conclusion_forever_fails(self):
        # Arrange
        api = FakeApi(base_routes())
        api.routes[("GET", f"repos/{REPOSITORY}/actions/runs/{RUN_ID}")] = [
            run(status="in_progress", conclusion=None), run(conclusion=None)]

        # Act
        with self.assertRaises(GitHubError) as raised:
            wait(api)

        # Assert
        self.assertEqual("child_result_timeout", raised.exception.code)

    def test_other_commit_fails(self):
        # Arrange
        api = FakeApi(base_routes())
        api.routes[("GET", f"repos/{REPOSITORY}/actions/runs/{RUN_ID}")] = [run(head_sha="3" * 40)]

        # Act
        with self.assertRaises(GitHubError) as raised:
            wait(api)

        # Assert
        self.assertEqual(("child_identity_mismatch", "head_sha"), (raised.exception.code, raised.exception.field))

    def test_failed_run_fails(self):
        # Arrange
        api = FakeApi(base_routes())
        api.routes[("GET", f"repos/{REPOSITORY}/actions/runs/{RUN_ID}")] = [run(conclusion="cancelled")]

        # Act
        with self.assertRaises(GitHubError) as raised:
            wait(api)

        # Assert
        self.assertEqual("child_failed", raised.exception.code)

    def test_long_run_exceeding_budget_fails(self):
        # Arrange
        api = FakeApi(base_routes())
        api.routes[("GET", f"repos/{REPOSITORY}/actions/runs/{RUN_ID}")] = [run(status="in_progress", conclusion=None)]

        # Act
        with self.assertRaises(GitHubError) as raised:
            wait(api)

        # Assert
        self.assertEqual("child_wait_timeout", raised.exception.code)


class VerifyJobsTests(unittest.TestCase):
    def verify(self, listing, required=("dependency-audit / Audit",)):
        api = FakeApi({("GET", f"repos/{REPOSITORY}/actions/runs/{RUN_ID}/jobs"): listing})
        coordinator.verify_jobs(api, REPOSITORY, RUN_ID, list(required))

    def test_required_matrix_rows_and_skipped_others_pass(self):
        # Arrange
        listing = jobs(("tests (mysql)", "success"), ("tests (mariadb)", "success"), ("benchmarks", "skipped"))

        # Act
        result = self.verify(listing, ["tests"])

        # Assert
        self.assertIsNone(result)

    def test_skipped_required_job_fails(self):
        # Arrange
        listing = jobs(("dependency-audit / Audit", "skipped"))

        # Act
        with self.assertRaises(GitHubError) as raised:
            self.verify(listing)

        # Assert
        self.assertIn("Required job", str(raised.exception))

    def test_cancelled_other_job_fails(self):
        # Arrange
        listing = jobs(("dependency-audit / Audit", "success"), ("extra", "cancelled"))

        # Act
        with self.assertRaises(GitHubError) as raised:
            self.verify(listing)

        # Assert
        self.assertIn("'extra'", str(raised.exception))

    def test_missing_required_job_fails(self):
        # Arrange
        listing = jobs(("other", "success"))

        # Act
        with self.assertRaises(GitHubError) as raised:
            self.verify(listing)

        # Assert
        self.assertEqual("jobs_unsuccessful", raised.exception.code)

    def test_listing_shorter_than_total_fails(self):
        # Arrange
        listing = jobs(("dependency-audit / Audit", "success"))
        listing["total_count"] = 2

        # Act
        with self.assertRaises(GitHubError) as raised:
            self.verify(listing)

        # Assert
        self.assertEqual("jobs_invalid", raised.exception.code)


class GuardTests(unittest.TestCase):
    def decide(self, env, raw=None, **arguments):
        api = FakeApi(base_routes(raw))
        values = {"maintenance": False, "correlation": "", "source_sha": ""}
        values.update(arguments)
        return guard.decide(api, env, **values)

    def maintenance_environment(self, **overrides):
        values = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/maintenance/10.x",
                  "GITHUB_SHA": MAINTENANCE,
                  "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/.github/workflows/dependency-audit.yml@refs/heads/maintenance/10.x"}
        values.update(overrides)
        return environment(**values)

    def test_ordinary_runs_are_unrestricted(self):
        # Arrange
        env = environment(GITHUB_EVENT_NAME="pull_request", GITHUB_REF="refs/pull/1/merge")

        # Act
        allowed = guard.decide(None, env, maintenance=False, correlation="", source_sha="")

        # Assert
        self.assertTrue(allowed)

    def test_dispatched_maintenance_run_proceeds(self):
        # Arrange
        env = self.maintenance_environment()

        # Act
        allowed = self.decide(env, maintenance=True, correlation="schedule-5-1", source_sha=MAINTENANCE)

        # Assert
        self.assertTrue(allowed)

    def test_maintenance_on_inactive_line_fails(self):
        # Arrange
        env = self.maintenance_environment()
        raw = policy_bytes(lambda value: value["lines"][1].update(state="inactive"))

        # Act
        with self.assertRaises(GitHubError) as raised:
            self.decide(env, raw, maintenance=True, correlation="schedule-5-1", source_sha=MAINTENANCE)

        # Assert
        self.assertIn("active lines besides main", str(raised.exception))

    def test_maintenance_with_other_commit_fails(self):
        # Arrange
        env = self.maintenance_environment()

        # Act
        with self.assertRaises(GitHubError) as raised:
            self.decide(env, maintenance=True, correlation="schedule-5-1", source_sha="4" * 40)

        # Assert
        self.assertIn("dispatched identity", str(raised.exception))

    def test_undeclared_workflow_fails(self):
        # Arrange
        env = self.maintenance_environment(
            GITHUB_WORKFLOW_REF=f"{REPOSITORY}/.github/workflows/other.yml@refs/heads/maintenance/10.x")

        # Act
        with self.assertRaises(GitHubError) as raised:
            self.decide(env, maintenance=True, correlation="schedule-5-1", source_sha=MAINTENANCE)

        # Assert
        self.assertIn("not a workload", str(raised.exception))

    def test_schedule_on_main_proceeds_only_while_main_is_active(self):
        # Arrange
        env = environment(GITHUB_WORKFLOW_REF=f"{REPOSITORY}/.github/workflows/dependency-audit.yml@refs/heads/main")
        raw = policy_bytes(lambda value: value["lines"][0].update(state="inactive"))

        # Act
        active = self.decide(env)
        inactive = self.decide(env, raw)

        # Assert
        self.assertEqual((True, False), (active, inactive))


if __name__ == "__main__":
    unittest.main()
