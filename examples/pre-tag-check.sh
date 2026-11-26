#!/usr/bin/env bash
# Purpose: check, before the release candidate starts, that the checked-out
# release line can release VERSION. Every check prints one OK or FAIL line;
# nothing is written. Needs git, gh (signed in) and python3.
#
# Usage: eng/pre-tag-check.sh VERSION    for example eng/pre-tag-check.sh 1.0.0-rc.1
set -euo pipefail

version="${1:?usage: eng/pre-tag-check.sh VERSION}"
root="$(git rev-parse --show-toplevel)"
workflow="${root}/.github/workflows/release-candidate.yml"

# WHY: The checks come from doka-labs/workflows at the commit this line's
# release workflow pins, so a maintenance line checks with its own version and
# no workflows checkout is needed. All calls of one line pin the same commit.
pins="$(grep -oE 'doka-labs/workflows/[^@ ]+@[0-9a-f]{40}' "${workflow}" | sed 's/.*@//' | sort -u)"
if [[ "$(printf '%s\n' "${pins}" | grep -c .)" -ne 1 ]]; then
  echo "${workflow} must pin exactly one doka-labs/workflows commit; found: ${pins:-none}" >&2
  exit 1
fi

tools="$(mktemp -d)"
trap 'rm -rf "${tools}"' EXIT

# WHY: git checks every fetched object against its hash, so the checkout is
# exactly the pinned commit.
git -C "${tools}" init -q
git -C "${tools}" fetch -q --depth 1 https://github.com/doka-labs/workflows.git "${pins}"
git -C "${tools}" -c advice.detachedHead=false checkout -q FETCH_HEAD
if [[ "$(git -C "${tools}" rev-parse HEAD)" != "${pins}" ]]; then
  echo "The fetched workflows commit is not ${pins}." >&2
  exit 1
fi

cd "${tools}"
python3 -E -s -m tools.release_publication pre-tag --root "${root}" --version "${version}"
