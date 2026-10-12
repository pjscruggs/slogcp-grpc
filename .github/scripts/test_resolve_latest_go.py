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

"""Regression checks for cached or malformed latest compiler selections."""

import unittest
from resolve_latest_go import select_latest


class LatestCompilerTests(unittest.TestCase):
    def test_exact_newest_stable_patch(self):
        releases = [{"version": "go1.27.1", "stable": True},
                    {"version": "go1.27.2", "stable": True},
                    {"version": "go1.28rc1", "stable": False}]
        self.assertEqual(select_latest(releases, "1.27.2"), "1.27.2")

    def test_missing_malformed_prerelease_or_stale_fails(self):
        for releases in ({}, [], [{"version": "go1.27.1", "stable": True}],
                         [{"version": "go1.28rc1", "stable": True}],
                         [{"version": "go1.28.0", "stable": "true"}]):
            with self.subTest(releases=releases), self.assertRaises(ValueError):
                select_latest(releases, "1.27.2")
