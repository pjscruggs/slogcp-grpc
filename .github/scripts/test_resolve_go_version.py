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

import unittest

import resolve_go_version


class ResolveGoVersionTests(unittest.TestCase):
    def test_parse_numeric_parts(self) -> None:
        self.assertEqual(resolve_go_version.parse_numeric_parts("1.26.6"), (1, 26, 6))
        self.assertIsNone(resolve_go_version.parse_numeric_parts("1.27rc1"))

    def test_selects_highest_matching_stable_patch_regardless_of_order(self) -> None:
        releases = [
            {"version": "go1.25.9", "stable": True},
            {"version": "go1.26.2", "stable": True},
            {"version": "go1.27rc1", "stable": False},
            {"version": "go1.26.6", "stable": True},
            {"version": "go1.26.4", "stable": True},
        ]

        result = resolve_go_version.select_version("1.26.0", releases)

        self.assertEqual(
            result,
            {"status": "resolved", "requested": "1.26", "version": "1.26.6"},
        )

    def test_major_minor_matching_does_not_accept_prefix_collisions(self) -> None:
        releases = [
            {"version": "go1.26.6", "stable": True},
            {"version": "go1.2.2", "stable": True},
        ]

        result = resolve_go_version.select_version("1.2.0", releases)

        self.assertEqual(result["version"], "1.2.2")

    def test_falls_back_to_newest_stable_lines_deterministically(self) -> None:
        releases = [
            {"version": "go1.25.8", "stable": True},
            {"version": "go1.26.4", "stable": True},
            {"version": "go1.25.10", "stable": True},
            {"version": "go1.26.6", "stable": True},
        ]

        result = resolve_go_version.select_version("1.28.0", releases)

        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["alias"], "stable")
        self.assertEqual(result["version"], "1.26.6")
        self.assertEqual(result["oldstable"], "1.25.10")

    def test_rejects_unparseable_go_directive(self) -> None:
        result = resolve_go_version.select_version("stable", [])

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["version"], "stable")


if __name__ == "__main__":
    unittest.main()
