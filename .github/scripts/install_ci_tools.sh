#!/usr/bin/env bash
# Copyright 2025-2026 Patrick J. Scruggs
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

set -euo pipefail
: "${CI_TOOLS_BIN:?Set CI_TOOLS_BIN to an absolute tools directory}"
[[ "$CI_TOOLS_BIN" == /* ]]
[[ "$(go env GOTOOLCHAIN)" == local ]]
echo "Building CI tools with $(go env GOVERSION) (GOTOOLCHAIN=local)."
(cd .github/tools && go mod tidy)
git diff --exit-code -- .github/tools/go.mod .github/tools/go.sum

tool_json="$(go mod edit -json .github/tools/go.mod)"
tool_packages="$(python3 -c 'import json, sys; print("\n".join(tool["Path"] for tool in json.load(sys.stdin).get("Tool", [])))' <<< "$tool_json")"
[[ -n "$tool_packages" ]]
mapfile -t declared_packages <<< "$tool_packages"
packages=()
if (( $# == 0 )); then
  packages=("${declared_packages[@]}")
else
  for requested in "$@"; do
    matches=()
    for package in "${declared_packages[@]}"; do
      if [[ "${package##*/}" == "$requested" ]]; then
        matches+=("$package")
      fi
    done
    if (( ${#matches[@]} != 1 )); then
      echo "Expected exactly one declared tool named $requested; found ${#matches[@]}." >&2
      exit 1
    fi
    packages+=("${matches[0]}")
  done
fi

mkdir -p "$CI_TOOLS_BIN"
GOBIN="$CI_TOOLS_BIN" go install -mod=readonly -modfile .github/tools/go.mod "${packages[@]}"
for package in "${packages[@]}"; do
  binary="$CI_TOOLS_BIN/${package##*/}"
  [[ -x "$binary" ]]
  go version -m "$binary" | awk 'NR == 1 || $1 == "path" || $1 == "mod" { print }'
done
