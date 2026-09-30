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

"""Check documentation in every tracked Go source directory and module."""

import argparse
import json
import pathlib
import subprocess
import sys


def tracked(pattern):
    output = subprocess.check_output(['git', 'ls-files', '-z', pattern])
    return [pathlib.PurePosixPath(p.decode()) for p in output.split(b'\0') if p]


def module_for(path, modules):
    for parent in path.parents:
        if parent / 'go.mod' in modules:
            return parent
    raise RuntimeError(f'No tracked Go module owns {path}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('godoclint', help='path to the pinned standalone godoclint binary')
    args = parser.parse_args()
    root = pathlib.Path.cwd()
    binary = pathlib.Path(args.godoclint).resolve()
    files = tracked('*.go')
    modules = set(tracked('*go.mod'))
    if not files:
        raise RuntimeError('No tracked Go files')
    groups = {}
    for path in files:
        module = module_for(path, modules)
        groups.setdefault((module, path.parent), []).append(path)

    checked = set()
    for (module, directory), members in sorted(groups.items()):
        workdir = root / module
        package = '.' if directory == module else './' + directory.relative_to(module).as_posix()
        listing = subprocess.run(['go', 'list', '-json', package], cwd=workdir,
                                 text=True, capture_output=True, check=True)
        info = json.loads(listing.stdout)
        included = set(info.get('GoFiles', []) + info.get('CgoFiles', []) +
                       info.get('TestGoFiles', []) + info.get('XTestGoFiles', []))
        ignored = set(info.get('IgnoredGoFiles', []))
        expected = {p.name for p in members}
        missing = expected - included - ignored
        if missing:
            raise RuntimeError(f'{directory}: unlisted Go files: {sorted(missing)}')

        subprocess.run([str(binary), '-config', str(root / '.godoc-lint.yaml'), package],
                       cwd=workdir, check=True)
        checked.update(p for p in members if p.name in included)
        # Inactive platform files still need declaration comments. An explicit
        # file argument analyzes them without relying on this runner's GOOS.
        for path in members:
            if path.name in ignored:
                subprocess.run([str(binary), '-config', str(root / '.godoc-lint.yaml'),
                                '-disable', 'require-pkg-doc', path.name],
                               cwd=root / directory, check=True)
                checked.add(path)

    if checked != set(files):
        raise RuntimeError(f'Coverage mismatch: {sorted(set(files) - checked)}')
    print(f'Godoc-Lint checked all {len(files)} tracked Go files in '
          f'{len(groups)} directories across {len({k[0] for k in groups})} modules.')


if __name__ == '__main__':
    try:
        main()
    except (subprocess.CalledProcessError, RuntimeError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
