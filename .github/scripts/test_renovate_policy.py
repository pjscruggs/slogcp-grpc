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

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VULNERABILITY = "isVulnerabilityAlert = true or $exists(vulnerabilityFixVersion)"
NON_VULNERABILITY = f"$not({VULNERABILITY})"
COMPILER_TOOLS = {
    "github.com/golangci/golangci-lint/v2",
    "golang.org/x/tools",
    "golang.org/x/vuln",
}
AUXILIARY_TOOLS = {
    "github.com/apache/skywalking-eyes",
    "github.com/godoc-lint/godoc-lint",
}


class RenovatePolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
        cls.package_rules = cls.config["packageRules"]
        cls.tools_go_mod = (ROOT / ".github/tools/go.mod").read_text(encoding="utf-8")
        cls.example_go_mod = (ROOT / ".examples/grpc/go.mod").read_text(encoding="utf-8")

    def find_rule(self, description: str) -> dict:
        for rule in self.package_rules:
            if rule.get("description") == description:
                return rule
        self.fail(f"rule not found: {description!r}")

    def test_updates_use_one_broad_window_and_can_refresh_outside_it(self) -> None:
        self.assertNotIn("config:semverAllWeekly", self.config["extends"])
        self.assertNotIn(":noUnscheduledUpdates", self.config["extends"])
        self.assertEqual(self.config["timezone"], "America/Chicago")
        self.assertEqual(self.config["schedule"], ["* * * * 1"])
        self.assertEqual(
            self.config["lockFileMaintenance"]["schedule"], ["* * * * 1"]
        )
        self.assertTrue(self.config["updateNotScheduled"])
        self.assertEqual(self.config["rebaseWhen"], "behind-base-branch")
        for rule in self.package_rules:
            self.assertNotIn("schedule", rule)
            self.assertNotIn("updateNotScheduled", rule)

    def test_local_dependents_are_tidied_without_removing_replacements(self) -> None:
        self.assertEqual(self.config["postUpdateOptions"], ["gomodTidyAll"])

    def test_security_eligibility_is_scoped_to_its_module(self) -> None:
        vulnerability = self.config["vulnerabilityAlerts"]
        self.assertTrue(vulnerability["enabled"])
        self.assertEqual(vulnerability["vulnerabilityFixStrategy"], "lowest")
        self.assertNotIn("automerge", vulnerability)
        self.assertTrue(self.config["osvVulnerabilityAlerts"])
        self.assertFalse(self.config["platformAutomerge"])

        security_floor = self.find_rule("Repair root dependency vulnerabilities and prepare a patch release")
        self.assertEqual(security_floor["matchManagers"], ["gomod"])
        self.assertEqual(security_floor["matchDatasources"], ["go"])
        self.assertEqual(security_floor["matchFileNames"], ["go.mod"])
        self.assertEqual(security_floor["matchDepTypes"], ["require", "indirect"])
        self.assertEqual(security_floor["matchJsonata"], [VULNERABILITY])
        self.assertTrue(security_floor["enabled"])
        self.assertFalse(security_floor["dependencyDashboardApproval"])
        self.assertTrue(security_floor["automerge"])
        self.assertEqual(security_floor["automergeType"], "pr")
        self.assertEqual(security_floor["automergeStrategy"], "squash")
        self.assertEqual(len(security_floor["bumpVersions"]), 1)
        bump = security_floor["bumpVersions"][0]
        self.assertEqual(bump["filePatterns"], ["version.go"])
        self.assertEqual(bump["bumpType"], "patch")
        self.assertEqual(len(bump["matchStrings"]), 1)
        self.assertIn("(?<version>", bump["matchStrings"][0])
        self.assertEqual(
            [rule["description"] for rule in self.package_rules if "bumpVersions" in rule],
            [security_floor["description"]],
        )

        for description, filename in (
            (
                "Keep CI tools vulnerability fixes separate from public dependency floor updates",
                ".github/tools/go.mod",
            ),
            (
                "Automatically repair example dependencies without a library release",
                ".examples/grpc/go.mod",
            ),
        ):
            with self.subTest(description=description):
                rule = self.find_rule(description)
                self.assertEqual(rule["matchFileNames"], [filename])
                self.assertEqual(rule["matchJsonata"], [VULNERABILITY])
                self.assertTrue(rule["automerge"])
                self.assertEqual(rule["automergeType"], "pr")
                self.assertEqual(rule["automergeStrategy"], "squash")
                self.assertNotIn("bumpVersions", rule)

    def test_indirect_lookup_uses_native_preset_and_narrow_routine_exceptions(self) -> None:
        self.assertIn("security:gomodIndirectSecurityUpdates", self.config["extends"])
        ordinary_exceptions = (
            "Compiler-sensitive Go CI tools update together",
            "Auxiliary Go CI tools update separately",
            "Routine latest-compatible updates for the checked-in module example",
        )
        for description in ordinary_exceptions:
            rule = self.find_rule(description)
            self.assertTrue(rule["enabled"])
            self.assertEqual(rule["matchJsonata"], [NON_VULNERABILITY])
        expected_enabled = {
            *ordinary_exceptions,
            "Repair root dependency vulnerabilities and prepare a patch release",
        }
        self.assertEqual(
            {rule["description"] for rule in self.package_rules if rule.get("enabled") is True},
            expected_enabled,
        )

    def test_dependency_roles_cannot_share_same_package_branch(self) -> None:
        prefixes = {
            tuple(rule["matchFileNames"]): rule["additionalBranchPrefix"]
            for rule in self.package_rules
            if "additionalBranchPrefix" in rule
        }
        self.assertEqual(
            prefixes,
            {
                ("go.mod",): "root-",
                (".github/tools/go.mod",): "tools-",
                (".examples/grpc/go.mod",): "examples-",
            },
        )

    def test_native_go_module_manages_each_ci_tool_once(self) -> None:
        self.assertNotIn("customManagers", self.config)
        declared_tools = set(
            re.findall(r"^\s*([^\s]+/cmd/[^\s]+)\s*$", self.tools_go_mod, re.MULTILINE)
        )
        expected_tools = {
            "github.com/apache/skywalking-eyes/cmd/license-eye",
            "github.com/godoc-lint/godoc-lint/cmd/godoclint",
            "github.com/golangci/golangci-lint/v2/cmd/golangci-lint",
            "golang.org/x/tools/cmd/goimports",
            "golang.org/x/vuln/cmd/govulncheck",
        }
        self.assertEqual(declared_tools, expected_tools)
        for module in COMPILER_TOOLS | AUXILIARY_TOOLS:
            with self.subTest(module=module):
                self.assertRegex(
                    self.tools_go_mod,
                    rf"(?m)^\s*{re.escape(module)}\s+v\d+\.\d+\.\d+\b",
                )

    def test_ci_dependency_groups_and_automatic_merges_are_explicit(self) -> None:
        actions = self.find_rule(
            "GitHub Actions updates stay separate from executable CI tools"
        )
        compiler = self.find_rule("Compiler-sensitive Go CI tools update together")
        auxiliary = self.find_rule("Auxiliary Go CI tools update separately")

        self.assertEqual(actions["matchManagers"], ["github-actions"])
        self.assertEqual(compiler["matchManagers"], ["gomod"])
        self.assertEqual(auxiliary["matchManagers"], ["gomod"])
        self.assertEqual(compiler["matchFileNames"], [".github/tools/go.mod"])
        self.assertEqual(auxiliary["matchFileNames"], [".github/tools/go.mod"])
        self.assertEqual(set(compiler["matchPackageNames"]), COMPILER_TOOLS)
        self.assertEqual(set(auxiliary["matchPackageNames"]), AUXILIARY_TOOLS)
        self.assertTrue(COMPILER_TOOLS.isdisjoint(AUXILIARY_TOOLS))
        for rule in (compiler, auxiliary):
            self.assertEqual(rule["matchJsonata"], [NON_VULNERABILITY])
        for rule in (actions, compiler, auxiliary):
            self.assertTrue(rule["automerge"])
            self.assertEqual(rule["automergeType"], "pr")
            self.assertEqual(rule["automergeStrategy"], "squash")

    def test_root_and_tools_go_directives_are_not_updated_independently(self) -> None:
        for description, file_name in (
            (
                "Do not automate the module's public Go compatibility floor",
                "go.mod",
            ),
            (
                "Do not update the CI tools module Go directive independently",
                ".github/tools/go.mod",
            ),
        ):
            with self.subTest(description=description):
                rule = self.find_rule(description)
                self.assertEqual(rule["matchFileNames"], [file_name])
                self.assertEqual(rule["matchDatasources"], ["golang-version"])
                self.assertEqual(rule["matchDepTypes"], ["golang"])
                self.assertFalse(rule["enabled"])

    def test_routine_root_dependencies_remain_disabled(self) -> None:
        rule = self.find_rule(
            "Do not propose routine root go.mod dependency floor updates"
        )
        self.assertEqual(rule["matchFileNames"], ["go.mod"])
        self.assertEqual(rule["matchDatasources"], ["go"])
        self.assertEqual(rule["matchDepTypes"], ["require", "indirect"])
        self.assertEqual(
            set(rule["matchUpdateTypes"]),
            {
                "major", "minor", "patch", "pin", "digest", "pinDigest",
                "lockFileMaintenance", "rollback", "replacement",
            },
        )
        self.assertFalse(rule["enabled"])

    def test_root_toolchain_and_example_updates_are_separate(self) -> None:
        root_toolchain = self.find_rule(
            "Keep the root go.mod toolchain directive on the latest released Go toolchain"
        )
        example_go = self.find_rule(
            "Keep checked-in example module go directives on the latest released Go version"
        )
        example_dependencies = self.find_rule(
            "Routine latest-compatible updates for the checked-in module example"
        )

        self.assertEqual(root_toolchain["matchDepTypes"], ["toolchain"])
        self.assertEqual(root_toolchain["matchFileNames"], ["go.mod"])
        self.assertEqual(example_go["matchDatasources"], ["golang-version"])
        self.assertEqual(example_go["matchDepTypes"], ["golang"])
        self.assertEqual(example_go["matchFileNames"], [".examples/grpc/go.mod"])
        self.assertEqual(example_dependencies["matchDatasources"], ["go"])
        self.assertEqual(
            example_dependencies["matchFileNames"], [".examples/grpc/go.mod"]
        )
        for rule in (root_toolchain, example_go):
            self.assertNotIn("matchJsonata", rule)
        self.assertEqual(example_dependencies["matchJsonata"], [NON_VULNERABILITY])
        for rule in (root_toolchain, example_go, example_dependencies):
            self.assertTrue(rule["automerge"])
            self.assertEqual(rule["automergeType"], "pr")
            self.assertEqual(rule["automergeStrategy"], "squash")
            self.assertNotIn("bumpVersions", rule)

    def test_examples_keep_local_module_requirement_pinned(self) -> None:
        rule = self.find_rule(
            "Do not update the local unpublished module requirement used by examples"
        )
        self.assertEqual(
            rule["matchPackageNames"],
            ["github.com/pjscruggs/slogcp-grpc"],
        )
        self.assertFalse(rule["enabled"])
        self.assertRegex(
            self.example_go_mod,
            r"(?m)^\s*github\.com/pjscruggs/slogcp-grpc v0\.0\.0-unpublished$",
        )
        self.assertRegex(
            self.example_go_mod,
            r"(?m)^replace github\.com/pjscruggs/slogcp-grpc => \.\./\.\.$",
        )


if __name__ == "__main__":
    unittest.main()
