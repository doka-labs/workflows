"""Minimal GitHub REST client over the GitHub CLI, using the job token from the environment."""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

# WHY: The newest REST API version; requests without the header use 2022-11-28.
# https://docs.github.com/en/rest/about-the-rest-api/api-versions
API_VERSION = "2026-03-10"
# WHY: The response bound of SafeMigrations' coordinator (eng/release/maintenance.py, MAX_JSON_BYTES).
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
FAILURE_CODES = frozenset((
    "failed", "github_http_error", "github_request_failed", "github_invalid_response", "github_response_limit",
    "github_timeout",
    "policy_invalid", "authority_mismatch", "branch_unprotected", "dispatch_invalid", "child_identity_mismatch",
    "child_visibility_timeout", "child_wait_timeout", "child_result_timeout", "child_failed", "child_state_invalid",
    "jobs_invalid", "jobs_unsuccessful"))


class GitHubError(RuntimeError):
    """A GitHub observation failed; the message is authored text, never an API body."""

    def __init__(self, message: str, *, code: str = "failed", http_status: int | None = None,
                 field: str | None = None):
        super().__init__(message)
        self.code = code if code in FAILURE_CODES else "failed"
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        self.field = field if isinstance(field, str) and re.fullmatch(r"[a-z_]{1,32}", field) else None


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

    def request(self, route: str, method: str = "GET", payload: dict | None = None) -> Any:
        """Return the parsed JSON body, or None for an empty 204, and raise on any non-2xx status."""
        command = ["gh", "api", "--include", "--hostname", "github.com", "--method", method,
                   "-H", "Accept: application/vnd.github+json", "-H", f"X-GitHub-Api-Version: {API_VERSION}", route]
        if payload is not None:
            command.extend(["--input", "-"])

        try:
            completed = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                                       capture_output=True, text=True, timeout=60, check=False)
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
        if not 200 <= http_status <= 299:
            raise GitHubError("GitHub request returned an unsuccessful HTTP status.", code="github_http_error",
                              http_status=http_status)

        if completed.returncode:
            raise GitHubError("GitHub request did not complete.", code="github_request_failed")

        if http_status == 204 and not body.strip():
            return None

        try:
            return json.loads(body, object_pairs_hook=_unique_object)
        except ValueError as error:
            raise GitHubError("GitHub response is not valid JSON.", code="github_invalid_response") from error


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
