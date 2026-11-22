"""Minimal GitHub REST client over the GitHub CLI, using the job token from the environment."""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from collections import deque
from typing import Any, Callable

# WHY: The newest REST API version; requests without the header use 2022-11-28.
# https://docs.github.com/en/rest/about-the-rest-api/api-versions
API_VERSION = "2026-03-10"
# WHY: The response bound of SafeMigrations' coordinator (eng/release/maintenance.py, MAX_JSON_BYTES).
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
FAILURE_CODES = frozenset((
    "failed", "github_http_error", "github_request_failed", "github_invalid_response", "github_response_limit",
    "github_timeout", "github_deadline",
    "policy_invalid", "authority_mismatch", "branch_unprotected", "dispatch_invalid", "child_identity_mismatch",
    "child_visibility_timeout", "child_wait_timeout", "child_result_timeout", "child_failed", "child_state_invalid",
    "jobs_invalid", "jobs_unsuccessful"))


class GitHubError(RuntimeError):
    """A GitHub observation failed; the message is authored text, never an API body."""

    def __init__(self, message: str, *, code: str = "failed", http_status: int | None = None,
                 field: str | None = None, rate_limited: bool = False, retry_after: int | None = None,
                 rate_reset: int | None = None):
        super().__init__(message)
        self.code = code if code in FAILURE_CODES else "failed"
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        self.field = field if isinstance(field, str) and re.fullmatch(r"[a-z_]{1,32}", field) else None
        self.rate_limited = rate_limited or http_status == 429 or retry_after is not None or rate_reset is not None
        self.retry_after = retry_after
        self.rate_reset = rate_reset


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    """Reject duplicate JSON names in API responses."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise GitHubError("GitHub response repeats a JSON property.", code="github_invalid_response")

        result[key] = value

    return result


class GitHub:
    """Send bounded REST requests through `gh api`, which reads GH_TOKEN from the environment."""

    def request(self, route: str, method: str = "GET", payload: dict | None = None, *, timeout: float = 60,
                on_exhausted: Callable[[int], None] | None = None) -> Any:
        """Return the parsed JSON body, or None for an empty 204, and raise on any non-2xx status."""
        command = ["gh", "api", "--include", "--hostname", "github.com", "--method", method,
                   "-H", "Accept: application/vnd.github+json", "-H", f"X-GitHub-Api-Version: {API_VERSION}", route]
        if payload is not None:
            command.extend(["--input", "-"])

        try:
            completed = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                                       capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as error:
            raise GitHubError(f"GitHub did not answer {method} {route.split('?', 1)[0]} in time.",
                              code="github_timeout") from error

        if len(completed.stdout) > MAX_RESPONSE_BYTES + 65536:
            raise GitHubError("GitHub response exceeded its size bound.", code="github_response_limit")

        # WHY: --include exposes the HTTP status, which the coordinator needs to tell a not-yet-visible run (404)
        # from other failures; stderr and error bodies are untrusted text (as in SafeMigrations' coordinator).
        headers, separator, body = completed.stdout.partition("\r\n\r\n")
        if not separator:
            headers, separator, body = completed.stdout.partition("\n\n")

        first = headers.splitlines()[0] if headers else ""
        status = re.fullmatch(r"HTTP/[0-9.]+ ([1-5][0-9]{2})(?: [^\r\n]*)?", first)
        if not separator or len(headers) > 65536 or status is None:
            code = "github_request_failed" if completed.returncode else "github_invalid_response"
            raise GitHubError("GitHub did not return a bounded HTTP response.", code=code)

        http_status = int(status[1])
        # WHY: GitHub uses these headers to tell clients when an exhausted primary or secondary budget resets.
        # https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api#exceeding-the-rate-limit
        header_values = {}
        for line in headers.splitlines()[1:]:
            key, colon, value = line.partition(":")
            if colon:
                header_values[key.lower()] = value.strip()

        rate_reset = (_numeric_header(header_values, "x-ratelimit-reset")
                      if header_values.get("x-ratelimit-remaining") == "0" else None)
        if not 200 <= http_status <= 299:
            rate_status = http_status in (403, 429)
            raise GitHubError("GitHub request returned an unsuccessful HTTP status.", code="github_http_error",
                              http_status=http_status, rate_limited=rate_status and _rate_message(body),
                              retry_after=_numeric_header(header_values, "retry-after") if rate_status else None,
                              rate_reset=rate_reset if rate_status else None)

        if completed.returncode:
            raise GitHubError("GitHub request did not complete.", code="github_request_failed")

        if rate_reset is not None and on_exhausted is not None:
            on_exhausted(rate_reset)

        if http_status == 204 and not body.strip():
            return None

        try:
            return json.loads(body, object_pairs_hook=_unique_object)
        except ValueError as error:
            raise GitHubError("GitHub response is not valid JSON.", code="github_invalid_response") from error


def _numeric_header(headers: dict[str, str], name: str) -> int | None:
    value = headers.get(name, "")
    return int(value) if re.fullmatch(r"[0-9]{1,12}", value) else None


def _rate_message(body: str) -> bool:
    try:
        data = json.loads(body)
    except ValueError:
        return False

    message = data.get("message") if isinstance(data, dict) else None
    return isinstance(message, str) and "rate limit" in message.lower()


class PacedGitHub:
    """Share a rolling request budget, a single in-flight call and a cooldown across coordinator threads."""

    def __init__(self, api, *, requests_per_hour: int, clock=time.monotonic, sleep=time.sleep, wall_clock=time.time):
        self._api = api
        self._limit = requests_per_hour
        self._clock = clock
        self._sleep = sleep
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._starts: deque[float] = deque()
        self._ready = float("-inf")

    def _defer_until_reset(self, reset: int) -> None:
        self._ready = max(self._ready, self._clock() + max(0.0, reset - self._wall_clock()))

    def _next_start(self, now: float) -> float:
        while self._starts and self._starts[0] <= now - 3600:
            self._starts.popleft()

        if len(self._starts) == self._limit:
            return max(self._ready, self._starts[0] + 3600)

        return self._ready

    def request(self, route: str, method: str = "GET", payload: dict | None = None, *,
                deadline: float | None = None) -> Any:
        """Pace every request and retry only a rate-limited read that fits its deadline."""
        until = self._clock() + 300 if deadline is None else deadline
        retries = 0
        while self._lock.acquire(timeout=max(0.0, until - self._clock())):
            try:
                now = self._clock()
                start = self._next_start(now)
                if max(now, start) >= until:
                    break

                if start <= now:
                    self._starts.append(now)
                    # WHY: GitHub recommends serial requests and at least a second between writes. Spacing every
                    # call also prevents read bursts while parallel child runs are filling in their identities.
                    # https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api
                    try:
                        return self._api.request(route, method, payload, timeout=min(60, until - now),
                                                 on_exhausted=self._defer_until_reset)
                    except GitHubError as error:
                        if not error.rate_limited:
                            raise

                        if error.rate_reset is not None:
                            self._defer_until_reset(error.rate_reset)
                        if error.retry_after is not None:
                            self._ready = max(self._ready, self._clock() + error.retry_after)
                        if error.rate_reset is None and error.retry_after is None:
                            self._ready = max(self._ready, self._clock() + 60 * 2 ** retries)

                        # WHY: Dispatch returns the new workflow run ID. Retry observations only, never replay a
                        # mutation when its result is unknown.
                        # https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event
                        if method != "GET" or retries >= 2:
                            raise

                        retries += 1
                        continue
                    finally:
                        self._ready = max(self._ready, self._clock() + 1)
            finally:
                self._lock.release()

            self._sleep(max(0.0, start - self._clock()))

        raise GitHubError("The GitHub request could not complete within its time budget.", code="github_deadline")


def failure_observation(error: BaseException) -> dict:
    """Describe a failure for a report with authored categories only, never API text or tokens."""
    result = {"verificationStatus": "failed", "failureCode": "failed"}
    if isinstance(error, GitHubError):
        result["failureCode"] = error.code
        if error.http_status is not None:
            result["httpStatus"] = error.http_status

        if error.field is not None:
            result["field"] = error.field

    return result
