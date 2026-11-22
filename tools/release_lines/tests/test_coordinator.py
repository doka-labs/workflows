import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.release_lines import coordinator, guard
from tools.release_lines.github import GitHubError, PacedGitHub

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

    def request(self, route, method="GET", payload=None, **options):
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
        clock = FakeClock()
        paced = PacedGitHub(api, requests_per_hour=coordinator.REQUESTS_PER_HOUR, clock=clock, sleep=clock.sleep)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            try:
                result = coordinator.coordinate(paced, env or environment(), "audit", output,
                                                dispatch=lambda *args, **kwargs: coordinator.dispatch_line(
                                                    *args, **kwargs, wait=lambda *a, **k: wait(a[0], clock)))
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
                  "GITHUB_WORKFLOW_REF":
                      f"{REPOSITORY}/.github/workflows/dependency-audit.yml@refs/heads/maintenance/10.x"}
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


class ConcurrencyTests(unittest.TestCase):
    def five_lines(self, value):
        for major in (9, 8, 7, 6):
            value["lines"].append({"sourceRef": f"refs/heads/maintenance/{major}.x", "role": "maintenance",
                                   "packageMajor": major, "channel": "stable-and-prerelease", "upstreamStage": "ga",
                                   "state": "active"})

    def test_every_line_waits_at_once_with_a_scaled_poll_interval(self):
        # Arrange
        import threading

        clock = FakeClock()
        api = PacedGitHub(FakeApi(base_routes(raw=policy_bytes(self.five_lines))), requests_per_hour=480,
                          clock=clock, sleep=clock.sleep)
        barrier = threading.Barrier(5, timeout=10)
        intervals = []

        def dispatch(*args, poll_seconds, **kwargs):
            intervals.append(poll_seconds)
            barrier.wait()
            return {"verificationStatus": "success"}

        # Act
        with tempfile.TemporaryDirectory() as directory:
            result = coordinator.coordinate(api, environment(), "audit", Path(directory) / "report.json",
                                            dispatch=dispatch)

        # Assert
        self.assertEqual("success", result["conclusion"])
        self.assertEqual([50.0] * 5, intervals)


class GitHubTimeoutTests(unittest.TestCase):
    def test_a_request_timeout_is_a_github_error(self):
        # Arrange
        import subprocess
        from unittest import mock

        from tools.release_lines.github import GitHub

        # Act
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["gh"], 60)):
            with self.assertRaises(GitHubError) as raised:
                GitHub().request("repos/doka-labs/Example/branches/main?x=1")

        # Assert
        self.assertEqual("github_timeout", raised.exception.code)
        self.assertNotIn("x=1", str(raised.exception))


class RequestPacingTests(unittest.TestCase):
    def test_shared_budget_covers_every_request(self):
        # Arrange
        route = "repos/example/project/actions/runs/1"
        fake = FakeApi({("GET", route): {}})
        clock = FakeClock()
        client = PacedGitHub(fake, requests_per_hour=2, clock=clock, sleep=clock.sleep)

        # Act
        client.request(route)
        client.request(route)
        with self.assertRaises(GitHubError) as raised:
            client.request(route, deadline=300)

        # Assert
        self.assertEqual("github_deadline", raised.exception.code)
        self.assertEqual(2, len(fake.calls))

    def test_throttled_dispatch_is_not_retried_and_delays_other_reads(self):
        # Arrange
        route = "repos/example/project/actions/workflows/audit.yml/dispatches"
        fake = FakeApi({("POST", route): GitHubError(
            "rate limited", code="github_http_error", http_status=429, retry_after=20),
            ("GET", route): {"ok": True}})
        clock = FakeClock()
        client = PacedGitHub(fake, requests_per_hour=480, clock=clock, sleep=clock.sleep)

        # Act
        with self.assertRaises(GitHubError):
            client.request(route, "POST", {"ref": "main"})
        result = client.request(route)

        # Assert
        self.assertEqual({"ok": True}, result)
        self.assertEqual(["POST", "GET"], [method for method, _, _ in fake.calls])
        self.assertEqual(20, clock.now)

    def test_get_uses_both_retry_headers_and_retains_the_phase_budget(self):
        # Arrange
        route = "repos/example/project/actions/runs/1"
        clock = FakeClock()
        throttled = GitHubError("rate limited", code="github_http_error", http_status=403,
                                retry_after=20, rate_reset=1030)
        fake = FakeApi({("GET", route): [throttled, {"ok": True}]})
        client = PacedGitHub(fake, requests_per_hour=480, clock=clock, sleep=clock.sleep,
                            wall_clock=lambda: 1000 + clock.now)

        # Act
        with self.assertRaises(GitHubError) as raised:
            client.request(route, deadline=29)
        result = client.request(route, deadline=31)

        # Assert
        self.assertEqual("github_deadline", raised.exception.code)
        self.assertEqual({"ok": True}, result)
        self.assertEqual(30, clock.now)
        self.assertEqual(2, len(fake.calls))

    def test_headerless_secondary_limit_retries_reads_with_exponential_backoff(self):
        # Arrange
        route = "repos/example/project/actions/runs/1"
        throttled = GitHubError("rate limited", code="github_http_error", http_status=429)
        fake = FakeApi({("GET", route): [throttled, throttled, {"ok": True}]})
        clock = FakeClock()
        client = PacedGitHub(fake, requests_per_hour=480, clock=clock, sleep=clock.sleep)

        # Act
        result = client.request(route)

        # Assert
        self.assertEqual({"ok": True}, result)
        self.assertEqual(180, clock.now)
        self.assertEqual(3, len(fake.calls))

    def test_transport_reads_case_insensitive_rate_headers(self):
        # Arrange
        import subprocess
        from unittest import mock

        from tools.release_lines.github import GitHub

        route = "repos/example/project/actions/runs/1"
        responses = [
            subprocess.CompletedProcess(["gh"], 1, stdout=(
                'HTTP/2 403\nRetry-After: 20\nX-RateLimit-Remaining: 0\nX-RateLimit-Reset: 1030\n\n'
                '{"message":"API rate limit exceeded"}'), stderr=""),
            subprocess.CompletedProcess(["gh"], 0, stdout=(
                'HTTP/2 200\nx-ratelimit-remaining: 0\nx-ratelimit-reset: 1060\n\n{"ok":true}'), stderr=""),
        ]
        clock = FakeClock()
        client = PacedGitHub(GitHub(), requests_per_hour=480, clock=clock, sleep=clock.sleep,
                            wall_clock=lambda: 1000 + clock.now)

        # Act
        with mock.patch("subprocess.run", side_effect=responses) as invoked:
            result = client.request(route, deadline=40)
            with self.assertRaises(GitHubError) as raised:
                client.request(route, deadline=59)

        # Assert
        self.assertEqual({"ok": True}, result)
        self.assertEqual("github_deadline", raised.exception.code)
        self.assertEqual([40, 10], [call.kwargs["timeout"] for call in invoked.call_args_list])

    def test_simultaneous_reads_keep_only_one_request_in_flight(self):
        # Arrange
        import concurrent.futures
        import threading

        clock = FakeClock()
        first_entered = threading.Event()
        second_started = threading.Event()
        finish_first = threading.Event()

        class Transport:
            def request(self, route, method, payload, **options):
                if route == "first":
                    first_entered.set()
                    if not finish_first.wait(5):
                        raise AssertionError("The test did not release the first request.")
                elif not finish_first.is_set():
                    raise AssertionError("The second request ran while the first was in flight.")
                return route

        client = PacedGitHub(Transport(), requests_per_hour=480, clock=clock, sleep=clock.sleep)

        def second_request():
            second_started.set()
            return client.request("second")

        # Act
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(client.request, "first")
            entered = first_entered.wait(5)
            second = executor.submit(second_request)
            started = second_started.wait(5)
            finish_first.set()
            results = (first.result(5), second.result(5))

        # Assert
        self.assertEqual((True, True), (entered, started))
        self.assertEqual(("first", "second"), results)
        self.assertEqual(1, clock.now)


class ScaledPollTests(unittest.TestCase):
    def test_late_visibility_succeeds_with_a_long_poll_interval(self):
        # Arrange
        routes = base_routes()
        routes[("GET", f"repos/{REPOSITORY}/actions/runs/{RUN_ID}")] = [
            GitHubError("missing", code="github_http_error", http_status=404), run()]
        api = FakeApi(routes)
        clock = FakeClock()
        workload = {"workflow": ".github/workflows/dependency-audit.yml", "requiredJobs": ["dependency-audit / Audit"],
                    "timeoutMinutes": 60}

        # Act
        result = coordinator.wait_for_run(api, REPOSITORY, RUN_ID, workload, "refs/heads/maintenance/10.x",
                                          MAINTENANCE, "schedule-5-1", record=lambda update: None, clock=clock,
                                          sleep=clock.sleep, poll_seconds=300)

        # Assert
        self.assertEqual("success", result["verificationStatus"])
        self.assertLess(clock.now, 300)


if __name__ == "__main__":
    unittest.main()
