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

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
PLAN = ROOT / ".github/scripts/plan_go_validation.sh"
BASH = shutil.which("bash")
if os.name == "nt":
    git_bash = pathlib.Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
    if git_bash.is_file():
        BASH = str(git_bash)


@unittest.skipUnless(BASH, "Bash is required to execute the actual CI planner")
class GoValidationPlanTests(unittest.TestCase):
    def plan(self, root="go 1.26.0\ntoolchain go1.27.0\n", example="go 1.27.1\n",
             tools="go 1.26.0\n", mode="requirements", manifests=(".examples/grpc/go.mod",)):
        with tempfile.TemporaryDirectory(prefix="go-validation-plan-") as directory:
            workspace = pathlib.Path(directory)
            sources = {"go.mod": root, ".github/tools/go.mod": tools,
                       **{manifest: example for manifest in manifests}}
            for name, source in sources.items():
                path = workspace / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("module fixture\n\n" + source, encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True, capture_output=True)
            subprocess.run(["git", "add", "."], cwd=workspace, check=True, capture_output=True)
            output = workspace / "outputs"
            env = dict(os.environ, GO_VALIDATION_MODE=mode, GITHUB_OUTPUT=output.as_posix())
            result = subprocess.run([BASH, PLAN.as_posix()], cwd=workspace, env=env,
                                    capture_output=True, text=True)
            values = dict(line.split("=", 1) for line in output.read_text().splitlines()) if output.exists() else {}
            return result, values

    def test_patch_latest_example_does_not_raise_the_library_runtime(self):
        result, values = self.plan()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values, {"mode": "requirements", "root_floor_spec": "1.26.x",
                                 "root_spec": "1.27.0", "example_spec": "1.27.1", "tools_spec": "1.27.1"})

    def test_future_tools_minimum_is_independent_of_the_root(self):
        result, values = self.plan(tools="go 1.30.2\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values["root_spec"], "1.27.0")
        self.assertEqual(values["root_floor_spec"], "1.26.x")
        self.assertEqual(values["tools_spec"], "1.30.2")

    def test_toolchain_preferences_participate_in_tools_build_selection(self):
        for tools, example, expected in (("go 1.26\ntoolchain go1.29.1\n", "go 1.28.0\n", "1.29.1"),
                                         ("go 1.26\n", "go 1.28\ntoolchain go1.29.3\n", "1.29.3")):
            with self.subTest(expected=expected):
                result, values = self.plan(tools=tools, example=example)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(values["tools_spec"], expected)

    def test_root_preference_can_be_the_newest_tools_build_requirement(self):
        result, values = self.plan(root="go 1.26\ntoolchain go1.31.4\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values["tools_spec"], "1.31.4")

    def test_missing_preferences_use_module_requirements(self):
        result, values = self.plan(root="go 1.26\n", example="go 1.26.1\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values["root_spec"], "1.26.0")
        self.assertEqual(values["tools_spec"], "1.26.1")

    def test_latest_lane_remains_dynamic_for_patch_and_minor_releases(self):
        result, values = self.plan(mode="latest")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values["root_floor_spec"], "1.26.x")
        self.assertEqual([values[name] for name in ("root_spec", "example_spec", "tools_spec")], ["stable"] * 3)

    def test_unexpected_or_missing_example_manifest_fails(self):
        for manifests in ((), (".examples/other/go.mod",), (".examples/grpc/go.mod", ".examples/other/go.mod")):
            with self.subTest(manifests=manifests):
                result, values = self.plan(manifests=manifests)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("exactly the tracked", result.stderr)
                self.assertFalse(values)

    def test_malformed_or_duplicate_directives_fail(self):
        for argument, source in (("root", "go 1.26\ngo 1.27\n"), ("tools", "go 1.26rc1\n"),
                                 ("example", ""), ("tools", "go 1.26\ntoolchain go1.27rc1\n"),
                                 ("root", "go 1.26\ntoolchain go1.27\ntoolchain go1.28\n")):
            with self.subTest(argument=argument, source=source):
                result, values = self.plan(**{argument: source})
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(values)

    def test_unsupported_mode_fails(self):
        result, values = self.plan(mode="anything")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsupported Go validation mode", result.stderr)
        self.assertFalse(values)


if __name__ == "__main__":
    unittest.main()
