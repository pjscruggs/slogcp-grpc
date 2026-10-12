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

"""Resolve the exact official stable compiler and reject stale releases."""

import json
import re
import sys
import urllib.request


def select_latest(releases, minimum):
    if not isinstance(releases, list) or not re.fullmatch(r"\d+\.\d+\.\d+", minimum):
        raise ValueError("Malformed releases or compiler requirement")
    versions = [item["version"][2:] for item in releases
                if isinstance(item, dict) and item.get("stable") is True and
                re.fullmatch(r"go\d+\.\d+\.\d+", str(item.get("version", "")))]
    if not versions:
        raise ValueError("Official release list has no stable compiler")
    numeric = lambda value: tuple(map(int, value.split(".")))
    latest = max(versions, key=numeric)
    if numeric(latest) < numeric(minimum):
        raise ValueError("Official stable compiler is below a declared requirement")
    return latest


def main():
    with urllib.request.urlopen("https://go.dev/dl/?mode=json", timeout=20) as response:
        content = response.read(1024 * 1024 + 1)
    if len(content) > 1024 * 1024:
        raise ValueError("Official Go release list exceeded its size bound")
    print(select_latest(json.loads(content), sys.argv[1]))


if __name__ == "__main__":
    main()
