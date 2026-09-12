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

read_required_directive() {
  local file="$1" directive="$2" values
  mapfile -t values < <(awk -v directive="$directive" '$1 == directive { print $2 }' "$file")
  if (( ${#values[@]} != 1 )); then
    echo "$file must contain exactly one $directive directive; found ${#values[@]}." >&2
    exit 1
  fi
  printf '%s\n' "${values[0]}"
}

normalize_version() {
  local version="${1#go}"
  if [[ ! "$version" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(\.((0|[1-9][0-9]*)))?$ ]]; then
    echo "Invalid Go version: $1" >&2
    exit 1
  fi
  printf '%s.%s.%s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[4]:-0}"
}

preferred_version() {
  local file="$1" minimum="$2" toolchains preferred
  mapfile -t toolchains < <(awk '$1 == "toolchain" { print $2 }' "$file")
  if (( ${#toolchains[@]} > 1 )); then
    echo "$file must contain at most one toolchain directive." >&2
    exit 1
  fi
  preferred="$minimum"
  if (( ${#toolchains[@]} == 1 )); then
    preferred="$(normalize_version "${toolchains[0]}")" || return 1
  fi
  printf '%s\n%s\n' "$minimum" "$preferred" | sort -V | tail -n 1
}

mapfile -t example_manifests < <(git ls-files -- ':(glob).examples/**/go.mod')
if (( ${#example_manifests[@]} != 1 )) || [[ "${example_manifests[0]:-}" != ".examples/grpc/go.mod" ]]; then
  echo "Validation requires exactly the tracked .examples/grpc/go.mod manifest." >&2
  printf 'Tracked example manifest: %s\n' "${example_manifests[@]:-<none>}" >&2
  exit 1
fi

root_go="$(normalize_version "$(read_required_directive go.mod go)")"
tools_go="$(normalize_version "$(read_required_directive .github/tools/go.mod go)")"
example_go="$(normalize_version "$(read_required_directive .examples/grpc/go.mod go)")"
root_preferred="$(preferred_version go.mod "$root_go")"
tools_preferred="$(preferred_version .github/tools/go.mod "$tools_go")"
example_preferred="$(preferred_version .examples/grpc/go.mod "$example_go")"
tools_required="$(printf '%s\n' "$root_preferred" "$example_preferred" "$tools_preferred" | sort -V | tail -n 1)"

case "$GO_VALIDATION_MODE" in
  requirements)
    root_spec="$root_preferred"
    example_spec="$example_preferred"
    tools_spec="$tools_required"
    ;;
  latest)
    root_spec=stable
    example_spec=stable
    tools_spec=stable
    ;;
  *)
    echo "Unsupported Go validation mode: $GO_VALIDATION_MODE" >&2
    exit 1
    ;;
esac

IFS=. read -r major minor _ <<< "$root_go"
root_floor_spec="$major.$minor.x"
{
  echo "mode=$GO_VALIDATION_MODE"
  echo "root_floor_spec=$root_floor_spec"
  echo "root_spec=$root_spec"
  echo "example_spec=$example_spec"
  echo "tools_spec=$tools_spec"
} >> "$GITHUB_OUTPUT"

echo "Validation mode: $GO_VALIDATION_MODE"
echo "Root compatibility series: $root_floor_spec"
echo "Preferred root input: $root_spec"
echo "Module example input: $example_spec"
echo "CI tools build input: $tools_spec (required $tools_required)"
