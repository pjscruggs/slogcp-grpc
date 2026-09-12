#!/usr/bin/env python3
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

from __future__ import annotations

import pathlib
import os
import re
import shutil
import subprocess
import textwrap
import unittest

import release_policy


ROOT = pathlib.Path(__file__).resolve().parents[2]
AUTO_RELEASE = (ROOT / ".github/workflows/auto-release.yml").read_text(encoding="utf-8")
VALIDATION = (ROOT / ".github/workflows/validation_pipeline.yml").read_text(
    encoding="utf-8"
)
LATEST_CANARY = (ROOT / ".github/workflows/latest-go-canary.yml").read_text(
    encoding="utf-8"
)
WORKFLOW_SOURCES = {
    path.name: path.read_text(encoding="utf-8")
    for path in (ROOT / ".github/workflows").glob("*.yml")
}
VERSION_SOURCE = (ROOT / "version.go").read_text(encoding="utf-8")
GO_PLAN = (ROOT / ".github/scripts/plan_go_validation.sh").read_text(encoding="utf-8")
TOOLS_INSTALLER = (ROOT / ".github/scripts/install_ci_tools.sh").read_text(encoding="utf-8")
ACTION_SMOKE = WORKFLOW_SOURCES["ci-action-smoke.yml"]


def validate_release_checkout_pins(release: str, validation: str) -> None:
    pattern = r"^\s*(?:-\s*)?uses:\s*['\"]?actions/checkout@([^\s'\"]+)"
    release_pins = re.findall(pattern, release, flags=re.MULTILINE)
    validation_pins = set(re.findall(pattern, validation, flags=re.MULTILINE))
    if len(release_pins) != 2 or any(not re.fullmatch(r"[a-f0-9]{40}", pin) for pin in release_pins):
        raise ValueError("Expected two immutable release checkout steps")
    if not set(release_pins) <= validation_pins:
        raise ValueError("Release checkout uses a revision not exercised by validation")


class CheckoutPinPolicyTests(unittest.TestCase):
    def test_matching_future_pins_are_accepted(self):
        for pin in ("a" * 40, "bc" * 20):
            with self.subTest(pin=pin):
                checkout = f"        uses: actions/checkout@{pin} # next version\n"
                validate_release_checkout_pins(checkout * 2, checkout)

    def test_unvalidated_or_missing_checkout_pins_are_rejected(self):
        checkout = f"        uses: actions/checkout@{'a' * 40}\n"
        different = f"        uses: actions/checkout@{'b' * 40}\n"
        for release, validation in (
            (checkout * 2, different),
            (checkout + different, checkout),
            (checkout, checkout),
            (checkout * 3, checkout),
            (checkout * 2, ""),
            (checkout + "        uses: actions/checkout@main\n", checkout),
            (checkout * 2 + "        uses: actions/checkout@main\n", checkout),
        ):
            with self.subTest(release=release, validation=validation), self.assertRaises(ValueError):
                validate_release_checkout_pins(release, validation)


class WorkflowPolicyTests(unittest.TestCase):
    def test_release_requires_explicit_version_intent(self) -> None:
        self.assertIn("paths: [ version.go ]", AUTO_RELEASE)
        self.assertIn("python .github/scripts/release_policy.py plan", AUTO_RELEASE)
        self.assertIn("INPUT_VERSION: ${{ inputs.version }}", AUTO_RELEASE)
        self.assertNotIn("security_pr_count", AUTO_RELEASE)
        self.assertNotIn("No root Go module metadata changed", AUTO_RELEASE)

    def test_release_checks_out_the_immutable_event_commit(self) -> None:
        self.assertEqual(AUTO_RELEASE.count("ref: ${{ github.sha }}"), 2)
        self.assertIn('git tag -s "$VERSION" "$GITHUB_SHA"', AUTO_RELEASE)
        self.assertNotIn("BEFORE_SHA", AUTO_RELEASE)

    def test_release_executes_its_policy_tests_before_planning(self) -> None:
        self.assertIn("-p 'test_release_*.py' -v", AUTO_RELEASE)
        self.assertLess(AUTO_RELEASE.index("name: Test Release Policy"),
                        AUTO_RELEASE.index("id: intent"))

    def test_release_uses_verified_tag_state_and_resumable_publication(self) -> None:
        for operation in ("tag-state", "verify-tag", "publish"):
            self.assertIn(f"python .github/scripts/release_policy.py {operation}", AUTO_RELEASE)
        self.assertNotIn("gh release view", AUTO_RELEASE)
        self.assertNotIn("--sort=-v:refname", AUTO_RELEASE)
        self.assertIn("if: steps.tag_state.outputs.tag_exists != 'true'", AUTO_RELEASE)

    def test_release_supports_optional_app_credentials(self) -> None:
        self.assertIn("actions/create-github-app-token@", AUTO_RELEASE)
        self.assertIn(
            "steps.release_app_token.outputs.token || github.token",
            AUTO_RELEASE,
        )
        self.assertIn("if: steps.release_app.outputs.available == 'true'", AUTO_RELEASE)
        release = AUTO_RELEASE.split("  release:\n", 1)[1]
        self.assertIn("permissions:\n      contents: write", release)
        self.assertNotIn("E2E_APP", AUTO_RELEASE)
        self.assertNotIn("id-token:", AUTO_RELEASE.split("  release:\n", 1)[1])
        self.assertNotIn("google-github-actions/", AUTO_RELEASE)
        validate_release_checkout_pins(AUTO_RELEASE, VALIDATION)

    def test_release_waits_for_the_reusable_validation_workflow(self) -> None:
        self.assertIn(
            "uses: ./.github/workflows/validation_pipeline.yml",
            AUTO_RELEASE,
        )
        self.assertIn("if: always() && needs.release_intent.outputs.should_release == 'true'", AUTO_RELEASE)
        self.assertIn("VALIDATION_RESULT: ${{ needs.release_preflight.result }}", AUTO_RELEASE)
        self.assertIn("VALIDATION_PASSED: ${{ needs.release_preflight.outputs.validation_passed }}", AUTO_RELEASE)
        self.assertLess(AUTO_RELEASE.index("name: Require Successful Release Validation"),
                        AUTO_RELEASE.index("name: Checkout Validated Commit"))

    def test_release_queue_preserves_pending_publications(self) -> None:
        self.assertIn("cancel-in-progress: false\n  queue: max", AUTO_RELEASE)

    def test_preflight_uses_full_history_and_requires_the_adapter_example(self) -> None:
        preflight = VALIDATION.split("  root_floor_validation:", 1)[0]
        self.assertIn("fetch-depth: 0", preflight)
        self.assertIn("name: Require current PR base", preflight)
        self.assertIn("git ls-remote", preflight)
        self.assertIn("git merge-base --is-ancestor", preflight)
        self.assertIn('git fetch --no-tags "https://github.com/${GITHUB_REPOSITORY}.git" "$current_base"', preflight)
        self.assertIn('validate_renovate_pr.py --base "$CURRENT_BASE_SHA"', preflight)
        self.assertIn("CURRENT_BASE_SHA: ${{ steps.current_base.outputs.sha }}", preflight)
        self.assertIn("bash .github/scripts/plan_go_validation.sh", preflight)
        self.assertIn("exactly the tracked .examples/grpc/go.mod", GO_PLAN)
        self.assertIn(".github/tools/go.mod", GO_PLAN)

    def test_validation_uses_exact_local_setup_go_runtimes(self) -> None:
        self.assertGreaterEqual(VALIDATION.count("GOTOOLCHAIN: local"), 3)
        self.assertEqual(VALIDATION.count("steps.setup_go.outputs.go-version"), 3)
        self.assertEqual(VALIDATION.count('go env GOTOOLCHAIN)" == "local"'), 3)
        self.assertIn("root_floor_spec", VALIDATION)
        self.assertIn("root_spec", VALIDATION)
        self.assertIn("example_spec", VALIDATION)

    def test_native_ci_tools_are_tidy_logged_and_executed(self) -> None:
        self.assertIn("(cd .github/tools && go mod tidy)", TOOLS_INSTALLER)
        self.assertIn("git diff --exit-code -- .github/tools/go.mod .github/tools/go.sum", TOOLS_INSTALLER)
        self.assertIn("go mod edit -json .github/tools/go.mod", TOOLS_INSTALLER)
        self.assertIn("go install -mod=readonly -modfile .github/tools/go.mod", TOOLS_INSTALLER)
        self.assertIn('go version -m "$binary"', TOOLS_INSTALLER)
        self.assertNotIn("go tool -modfile", VALIDATION)
        for tool in ("golangci-lint", "goimports", "govulncheck", "license-eye"):
            with self.subTest(tool=tool):
                self.assertIn(f'"$CI_TOOLS_BIN/{tool}"', VALIDATION)

    def test_tools_are_built_before_restoring_the_module_compiler(self) -> None:
        root = VALIDATION.split("  validation:", 1)[1].split("  example_validation:", 1)[0]
        example = VALIDATION.split("  example_validation:", 1)[1].split("  module_local_validation_policy:", 1)[0]
        for source, setup in ((root, "Set up preferred root Go runtime"), (example, "Set up module example Go runtime")):
            with self.subTest(setup=setup):
                self.assertIn("go-version: ${{ needs.validation_preflight.outputs.tools_spec }}", source)
                self.assertIn("EXPECTED_VERSION: ${{ steps.setup_tools_go.outputs.go-version }}", source)
                self.assertLess(source.index("bash .github/scripts/install_ci_tools.sh"), source.index(setup))
                self.assertIn('export CI_TOOLS_BIN="$RUNNER_TEMP/ci-tools"', source)
                self.assertIn('echo "CI_TOOLS_BIN=$CI_TOOLS_BIN" >> "$GITHUB_ENV"', source)
                self.assertIn("cache: false", source.split(setup, 1)[1])
        self.assertIn("install_ci_tools.sh goimports govulncheck", example)

    def test_root_and_example_validation_are_independent_and_fail_closed(self) -> None:
        self.assertIn("go test -mod=readonly -race -count=1 ./...", VALIDATION)
        self.assertIn("name: Validate Module Example", VALIDATION)
        self.assertIn("(cd .examples/grpc && go mod tidy)", VALIDATION)
        self.assertIn(
            "(cd .examples/grpc && go test -race -count=1 ./...)",
            VALIDATION,
        )
        self.assertIn('require_success "Root compatibility validation"', VALIDATION)
        self.assertIn('require_success "Preferred root validation"', VALIDATION)
        self.assertIn('require_success "Module example validation"', VALIDATION)
        self.assertNotIn("sync_example_go_versions", VALIDATION)

    def test_required_aggregate_name_and_release_preflight_are_preserved(self) -> None:
        self.assertEqual(VALIDATION.count("name: Module Local Validation Policy"), 1)
        self.assertIn("uses: ./.github/workflows/validation_pipeline.yml", AUTO_RELEASE)

    def test_action_updates_execute_the_same_pin_as_the_release_workflow(self) -> None:
        action = r"actions/create-github-app-token@([a-f0-9]{40})"
        self.assertEqual(re.findall(action, AUTO_RELEASE), re.findall(action, ACTION_SMOKE))
        self.assertEqual(len(re.findall(action, ACTION_SMOKE)), 1)
        self.assertIn("github-api-url: ${{ steps.fixture.outputs.base_url }}", ACTION_SMOKE)
        self.assertIn('--github-only --app-token "$APP_TOKEN"', ACTION_SMOKE)
        self.assertNotIn("${{ secrets.", ACTION_SMOKE)
        self.assertNotIn("id-token: write", ACTION_SMOKE)
        self.assertNotIn("google-github-actions/", ACTION_SMOKE)

    def test_action_smoke_is_required_and_unexpected_skips_fail(self) -> None:
        self.assertIn("uses: ./.github/workflows/ci-action-smoke.yml", VALIDATION)
        self.assertIn("      - ci_action_smoke", VALIDATION)
        self.assertIn('require_success "Candidate CI action smoke tests" "$ACTION_RESULT"', VALIDATION)
        self.assertIn('if [[ "$ACTION_PASSED" != "true" ]]', VALIDATION)
        self.assertIn('test "${#results[@]}" = 4', ACTION_SMOKE)
        self.assertIn('for result in "${results[@]}"; do test "$result" = success; done', ACTION_SMOKE)
        self.assertIn("if: always()", ACTION_SMOKE)

    def test_latest_canary_reuses_complete_validation_and_requires_equal_versions(self) -> None:
        self.assertIn("uses: ./.github/workflows/validation_pipeline.yml", LATEST_CANARY)
        self.assertIn("go_validation_mode: latest", LATEST_CANARY)
        self.assertIn("needs.validate_latest_go.result", LATEST_CANARY)
        self.assertIn("needs.validate_latest_go.outputs.validation_passed", LATEST_CANARY)
        self.assertIn("Module Latest Go Policy", LATEST_CANARY)
        self.assertIn('[[ "$ROOT_VERSION" != "$EXAMPLE_VERSION" ]]', LATEST_CANARY)
        self.assertNotIn("id-token: write", LATEST_CANARY)

    def test_every_referenced_local_ci_script_exists(self) -> None:
        references: set[str] = set()
        for source in WORKFLOW_SOURCES.values():
            references.update(
                re.findall(r"\.github/scripts/[A-Za-z0-9_.\-/]+", source)
            )

        self.assertTrue(references)
        for reference in sorted(references):
            with self.subTest(reference=reference):
                self.assertTrue((ROOT / reference).is_file(), reference)

    def test_version_uses_the_publisher_canonical_module_version(self) -> None:
        self.assertRegex(release_policy.version_of(VERSION_SOURCE), r"^v\d+\.\d+\.\d+$")


class ValidationResultGuardTests(unittest.TestCase):
    def execute(self, source: str, marker: str, values: dict[str, str]) -> int:
        git_bash = pathlib.Path("C:/Program Files/Git/bin/bash.exe")
        bash = str(git_bash) if git_bash.is_file() else shutil.which("bash")
        if not bash:
            self.skipTest("Bash is required to execute the actual workflow result guard")
        step = source.split(marker, 1)[1]
        body = textwrap.dedent(step.split("        run: |\n", 1)[1].split("\n      - name:", 1)[0])
        return subprocess.run(
            [bash, "--noprofile", "--norc", "-c", body],
            env={**os.environ, **values, "GITHUB_OUTPUT": "/dev/null"},
            capture_output=True, text=True, check=False,
        ).returncode

    def test_action_guard_rejects_each_failed_or_skipped_expected_step(self):
        marker = "      - name: Require Successful Action Execution\n"
        self.assertEqual(self.execute(ACTION_SMOKE, marker, {"RESULTS": " ".join(["success"] * 4)}), 0)
        for lane in range(4):
            for outcome in ("failure", "skipped", "cancelled", "neutral", ""):
                with self.subTest(lane=lane, outcome=outcome):
                    results = ["success"] * 4
                    results[lane] = outcome
                    self.assertNotEqual(self.execute(ACTION_SMOKE, marker, {"RESULTS": " ".join(results)}), 0)

    def test_aggregate_rejects_each_failed_or_skipped_expected_job(self):
        marker = "      - name: Evaluate adapter local validation result\n"
        values = {"PREFLIGHT_RESULT": "success", "FLOOR_RESULT": "success", "ROOT_RESULT": "success",
                  "EXAMPLE_RESULT": "success", "ACTION_RESULT": "success", "ACTION_PASSED": "true",
                  "MODE": "requirements", "ROOT_VERSION": "1.26.6", "EXAMPLE_VERSION": "1.27.1"}
        self.assertEqual(self.execute(VALIDATION, marker, values), 0)
        for key in ("PREFLIGHT_RESULT", "FLOOR_RESULT", "ROOT_RESULT", "EXAMPLE_RESULT", "ACTION_RESULT"):
            for outcome in ("failure", "skipped", "cancelled", "neutral", ""):
                with self.subTest(key=key, outcome=outcome):
                    self.assertNotEqual(self.execute(VALIDATION, marker, {**values, key: outcome}), 0)
        for output in ("false", "", "success"):
            with self.subTest(output=output):
                self.assertNotEqual(self.execute(VALIDATION, marker, {**values, "ACTION_PASSED": output}), 0)

    def test_release_requires_success_and_explicit_validation_output(self):
        marker = "      - name: Require Successful Release Validation\n"
        values = {"VALIDATION_RESULT": "success", "VALIDATION_PASSED": "true", "E2E_RESULT": "success"}
        self.assertEqual(self.execute(AUTO_RELEASE, marker, values), 0)
        for result in ("failure", "skipped", "cancelled", "neutral", ""):
            with self.subTest(result=result):
                self.assertNotEqual(self.execute(AUTO_RELEASE, marker, {**values, "VALIDATION_RESULT": result}), 0)
                self.assertNotEqual(self.execute(AUTO_RELEASE, marker, {**values, "E2E_RESULT": result}), 0)
        for output in ("false", "", "success"):
            with self.subTest(output=output):
                self.assertNotEqual(self.execute(AUTO_RELEASE, marker, {**values, "VALIDATION_PASSED": output}), 0)
        self.assertNotEqual(self.execute(AUTO_RELEASE, marker, {}), 0)

    def test_optional_release_app_credentials_must_be_configured_together(self):
        marker = "      - name: Detect Release App Credentials\n"
        for app_id, private_key in (("", ""), ("fixture-app", "fixture-private-key")):
            with self.subTest(app_id=app_id, private_key=private_key):
                self.assertEqual(self.execute(AUTO_RELEASE, marker, {
                    "RELEASE_APP_ID": app_id, "RELEASE_APP_PRIVATE_KEY": private_key,
                }), 0)
        for app_id, private_key in (("fixture-app", ""), ("", "fixture-private-key")):
            with self.subTest(app_id=app_id, private_key=private_key):
                self.assertNotEqual(self.execute(AUTO_RELEASE, marker, {
                    "RELEASE_APP_ID": app_id, "RELEASE_APP_PRIVATE_KEY": private_key,
                }), 0)


class LicensePolicyTests(unittest.TestCase):
    def test_license_validation_uses_versioned_policy(self):
        self.assertNotIn("date +%Y", VALIDATION)
        self.assertNotIn("steps.year.outputs.YEAR", VALIDATION)
        self.assertNotRegex(VALIDATION, r"sed[^\n]*copyright-year:")
        self.assertIn("header check", VALIDATION)
        self.assertIn(".licenserc.yaml", VALIDATION)


if __name__ == "__main__":
    unittest.main()
