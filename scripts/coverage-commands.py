#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This file is part of nvme-cli.
"""Report which commands no test runs, from a coverage build.

If the tests of a coverage build never ran a command, the callback of its
`struct command` (see CONTRIBUTING.md, "Adding commands and plugins") has no
covered line. So this takes the commands a build ships from the metadata the
binary itself emits, maps each to its callback by reading the command tables
out of the sources, looks the callback up in a gcovr report, and prints what
nothing ran, per component, worst first:

    amzn: 0 of 2 commands run by a test, 7% of the plugin's lines covered
    Commands without a test:
      nvme amzn id-ctrl
      nvme amzn stats
    Largest uncovered functions (lines covered / total):
      get_stats(): 19 / 71
      ...

A component is a plugin, or the built-in commands of the CLI ("nvme"). Its
line percentage covers every file of the component, not only its commands.
The deprecated commands only run their replacement, so they are left out
unless --deprecated is given.

A command counts as run only if its callback got past its argument parsing.
Printing the help of a command, and dumping the command metadata, run the
top of every callback without the command doing anything, so reaching a line
there is not a test having exercised it.

Usage: coverage-commands.py BUILD_DIR [options]
       coverage-commands.py --json REPORT [--metadata FILE] [options]

BUILD_DIR is a build configured with -Db_coverage=true whose tests have run.
See Documentation/TESTING.md for the whole process.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# The built-in commands have no plugin prefix.
BUILTIN = 'nvme'

# A `struct command`/`struct plugin` definition and its initializer.
_DEF_RE = re.compile(r'\bstruct\s+(command|plugin)\s+\w+\s*=\s*\{(.*?)\n\};',
                     re.DOTALL)
_NAME_RE = re.compile(r'\.name\s*=\s*"([^"]*)"')
_FN_RE = re.compile(r'\.fn\s*=\s*(\w+)')

# The deprecated built-ins come from macros: DEPRECATED() runs the generic
# forwarder, DEPRECATED_FN() its own callback (src/nvme-cmds-deprecated.c).
_DEPRECATED_RE = re.compile(r'\bDEPRECATED\s*\(\s*"([^"]*)"', re.DOTALL)
_DEPRECATED_FN_RE = re.compile(
    r'\bDEPRECATED_FN\s*\(\s*"([^"]*)"\s*,.*?,.*?,.*?,\s*(\w+)', re.DOTALL)
_FORWARDER = 'run_replacement'

# A `#define` which renames a callback.
_DEFINE_RE = re.compile(r'^\s*#\s*define\s+(\w+)\s+(\w+)\s*$', re.MULTILINE)


def component_of(path):
    """The plugin directory a source file belongs to, or None if it is not
    a plugin's."""
    parts = Path(path).parts
    if len(parts) > 2 and parts[0] == 'plugins':
        return parts[1]
    return None


def read_sources(root):
    """{path: text} of the sources which hold a command table."""
    sources = {}
    for pattern in ('plugins/*/*.c', 'src/*.c'):
        for path in sorted(root.glob(pattern)):
            sources[path.relative_to(root).as_posix()] = path.read_text(
                encoding='utf-8', errors='replace')
    return sources


def plugin_names(sources):
    """{plugin directory: the .name of its `struct plugin`}. A command
    belongs to the plugin of its directory, so that the commands of a
    multi-file plugin all land in the same component."""
    names = {}
    for path, text in sources.items():
        directory = component_of(path)
        if directory is None:
            continue
        for kind, body in _DEF_RE.findall(text):
            name = _NAME_RE.search(body)
            if kind == 'plugin' and name:
                names[directory] = name.group(1)
    return names


def components_of(sources):
    """{path: the component of the file}."""
    plugins = plugin_names(sources)
    return {path: plugins.get(component_of(path), BUILTIN)
            for path in sources}, plugins


def resolve_fn(fn, path, sources, components):
    """The (file, name) a callback is defined in: another file of the same
    multi-file plugin may define it, and a `#define` may rename it."""
    others = [p for p in sources if components.get(p) == components.get(path)
              and p != path]
    seen = set()
    while fn not in seen:
        seen.add(fn)
        for candidate in [path] + others:
            if re.search(r'^[^\n]*\b' + re.escape(fn) + r'\s*\(int\s',
                         sources[candidate], re.MULTILINE):
                return candidate, fn
        renamed = None
        for candidate in [path] + others:
            for name, target in _DEFINE_RE.findall(sources[candidate]):
                if name == fn:
                    renamed = target
        if renamed is None:
            break
        fn = renamed
    return path, fn


class Callback:
    """The callback of a command: the function @name of @file, the line its
    own work starts after, and the functions it hands the work to.

    `nvme-cli - usage` prints the help of every command, and a command run
    without a usable device fails in its argument parsing. Both run the top
    of a callback, so neither means a test exercised the command.
    @work_after is the last line of the callback's call into the argument
    parsing, with the error check right after it; a covered line past it is
    the command doing its work. A callback which only forwards to a shared
    helper (`__id_ctrl()`) has nothing past it, so @delegates holds the
    functions it hands over to, whose coverage is its own work."""

    def __init__(self, file, name, work_after=None, delegates=()):
        self.file = file
        self.name = name
        self.work_after = work_after
        self.delegates = tuple(delegates)


# What a callback calls to parse its arguments, and with them open the
# device: whatever it hands its own argc and argv to. That is
# parse_and_open() for most commands, parse_args() for one without a
# device, a plugin's own wrapper of either (micron_parse_options()), or the
# shared helper a callback forwards to wholesale (__id_ctrl()).
_ENTRY_RE = re.compile(r'\b(\w+)\s*\(([^;{}]*)', re.DOTALL)


def _close_paren(text, start):
    """The index of the ')' which closes the '(' at @start."""
    depth = 0
    for index in range(start, len(text)):
        if text[index] == '(':
            depth += 1
        elif text[index] == ')':
            depth -= 1
            if not depth:
                return index
    return None


def function_body(text, name):
    """The (first line, [lines]) of the definition of a function, or None.

    A definition is a parameter list followed by '{'; a declaration and a
    call are not. The body ends at the closing brace in column 0, which is
    how a function ends in this tree's style."""
    for match in re.finditer(r'\b' + re.escape(name) + r'\s*\(', text):
        close = _close_paren(text, match.end() - 1)
        if close is None:
            continue
        rest = text[close + 1:close + 32].lstrip(' \t\n')
        if not rest.startswith('{'):
            continue
        start = text.count('\n', 0, match.start()) + 1
        body = []
        for line in text.split('\n')[start - 1:]:
            body.append(line)
            if line == '}' and len(body) > 1:
                break
        return start, body
    return None


def skip_error_check(body, last):
    """The last line of the `if (err) return err;` which follows the entry
    call, or @last if no error check follows it. That check runs when the
    parsing failed, so it is not the command doing its work either."""
    index = last + 1
    while index < len(body) and not body[index].strip():
        index += 1
    if index >= len(body) or not re.match(r'\s*if\s*\(', body[index]):
        return last
    if body[index].rstrip().endswith('{'):
        depth = 1
        while index + 1 < len(body) and depth:
            index += 1
            depth += body[index].count('{') - body[index].count('}')
    else:
        while index + 1 < len(body) and ';' not in body[index]:
            index += 1
    check = ' '.join(body[last + 1:index + 1])
    # Only a check which leaves the function is the error check.
    if not re.search(r'\b(return|goto)\b', check):
        return last
    return index


def callback_of(file, name, text):
    """The Callback of a command: where its own work starts in the source,
    and the functions it delegates to."""
    found = function_body(text, name)
    if not found:
        return Callback(file, name)
    start, body = found

    # The body without its own signature, which also names argc and argv.
    for offset in range(1, len(body)):
        # A call may span lines, so look at the whole statement.
        last = offset
        while last < len(body) - 1 and ';' not in body[last]:
            last += 1
        statement = ' '.join(body[offset:last + 1])
        call = _ENTRY_RE.search(statement)
        if not (call and re.search(r'\bargc\b', statement)
                and re.search(r'\bargv\b', statement)):
            continue
        # A function the call hands the work to, e.g. the printer a
        # command passes to __id_ctrl().
        delegates = [word for word in re.findall(r'\b\w+\b', call.group(2))
                     if word != name and function_body(text, word)]
        return Callback(file, name, start + skip_error_check(body, last),
                        delegates)
    return Callback(file, name)


def read_callbacks(root):
    """{(component, command name): Callback} of the whole tree."""
    sources = read_sources(root)
    components, plugins = components_of(sources)

    callbacks = {}
    for path, text in sources.items():
        component = components[path]
        found = []
        for kind, body in _DEF_RE.findall(text):
            name = _NAME_RE.search(body)
            fn = _FN_RE.search(body)
            if kind == 'command' and name and fn:
                found.append((name.group(1), fn.group(1)))
        for name, fn in _DEPRECATED_FN_RE.findall(text):
            found.append((name, fn))
        fn_names = {name for name, _ in found}
        for name in _DEPRECATED_RE.findall(text):
            if name not in fn_names:  # DEPRECATED_FN matches both
                found.append((name, _FORWARDER))
        for name, fn in found:
            file, func = resolve_fn(fn, path, sources, components)
            callbacks[(component, name)] = callback_of(file, func,
                                                       sources[file])
    return callbacks, plugins


def dump_metadata(build_dir):
    """The command metadata of the nvme binary of a build.

    Collecting the options of a command runs its callback as far as the
    argument parsing, so this run would add coverage to the very callbacks
    we are about to judge. GCOV_PREFIX sends its counters to a directory of
    its own instead, leaving the data of the build untouched."""
    binary = Path(build_dir) / 'nvme'
    if not binary.exists():
        raise SystemExit(f'{binary} not found: is {build_dir} built?')
    with tempfile.TemporaryDirectory(prefix='nvme-metadata-') as counters:
        env = dict(os.environ, GCOV_PREFIX=counters, GCOV_PREFIX_STRIP='99')
        out = subprocess.run([str(binary), 'utils', 'dump-command-metadata'],
                             check=True, capture_output=True, text=True,
                             env=env).stdout
    return json.loads(out)


def read_metadata(data):
    """The commands a build ships: [(component, name, deprecated)]."""
    commands = [(BUILTIN, c['name'], bool(c.get('deprecated')))
                for c in data.get('commands', [])]
    for plugin in data.get('plugins', []):
        commands += [(plugin['name'], c['name'], bool(c.get('deprecated')))
                     for c in plugin.get('commands', [])]
    return commands


def run_gcovr(root, build_dir):
    """The gcovr JSON report of a build."""
    cmd = ['gcovr', '-r', str(root), str(build_dir), '--json', '-']
    try:
        out = subprocess.run(cmd, check=True, capture_output=True,
                             text=True).stdout
    except FileNotFoundError:
        raise SystemExit('gcovr not found; install it or pass --json')
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f'gcovr failed:\n{exc.stderr}')
    return json.loads(out)


class Coverage:
    """The line and function coverage of a gcovr report."""

    def __init__(self, report):
        self.lines = {}      # file -> {line number: times run}
        self.functions = {}  # file -> {function: (first line, times run)}
        for entry in report.get('files', []):
            path = entry['file']
            self.lines[path] = {line['line_number']: line.get('count', 0)
                                for line in entry.get('lines', [])
                                if not line.get('gcovr/noncode')}
            funcs = {}
            for func in entry.get('functions', []):
                name = func.get('name') or func.get('demangled_name')
                if name:
                    funcs[name] = (func.get('lineno',
                                            func.get('start_line', 0)),
                                   func.get('execution_count', 0))
            self.functions[path] = funcs

    def extent(self, path, name):
        """The (first, last) line a function covers, or None if the report
        does not hold it. gcovr reports only where a function starts, so it
        reaches to the start of the next one."""
        funcs = self.functions.get(path, {})
        if name not in funcs:
            return None
        start = funcs[name][0]
        later = sorted(line for line, _ in funcs.values() if line > start)
        return start, later[0] - 1 if later else max(
            self.lines.get(path, {start: 0}), default=start)

    def covered_lines(self, path, name):
        """The lines of a function which ran."""
        extent = self.extent(path, name)
        if extent is None:
            return set()
        first, last = extent
        return {line for line, count in self.lines.get(path, {}).items()
                if first <= line <= last and count > 0}

    def function_lines(self, path, name):
        """The (covered, total) lines of a function, or None if the report
        does not hold it."""
        extent = self.extent(path, name)
        if extent is None:
            return None
        first, last = extent
        lines = [count for line, count in self.lines.get(path, {}).items()
                 if first <= line <= last]
        return sum(1 for count in lines if count > 0), len(lines)

    def work_lines(self, callback):
        """The lines of a callback's own work which ran: those past its
        call into the argument parsing (see Callback)."""
        covered = self.covered_lines(callback.file, callback.name)
        if callback.work_after is None:
            return covered
        return {line for line in covered if line > callback.work_after}

    def ran(self, callback, baseline=None):
        """Whether a test ran a command: its callback got past its argument
        parsing, or a function it hands the work to ran.

        Lines a @baseline report already covers do not count. The baseline
        is a run which only printed the help of every command and ran them
        without a device, so what it covers is a command that did nothing."""
        work = self.work_lines(callback)
        delegated = [self.covered_lines(callback.file, name)
                     for name in callback.delegates]
        if baseline is not None:
            work -= baseline.work_lines(callback)
            delegated = [lines - baseline.covered_lines(callback.file, name)
                         for lines, name in zip(delegated,
                                                callback.delegates)]
        return bool(work or any(delegated))

    def files_of(self, component, plugins):
        """The files of a component the report holds."""
        for path in self.lines:
            directory = component_of(path)
            name = plugins.get(directory) if directory else BUILTIN
            if name == component:
                yield path

    def component_lines(self, component, plugins):
        """The (covered, total) lines of every file of a component."""
        covered = total = 0
        for path in self.files_of(component, plugins):
            lines = self.lines[path]
            total += len(lines)
            covered += sum(1 for count in lines.values() if count > 0)
        return covered, total

    def uncovered_functions(self, component, plugins):
        """The functions of a component with an uncovered line, by the
        number of uncovered lines: [(file, name, covered, total)]."""
        out = []
        for path in self.files_of(component, plugins):
            for func in self.functions.get(path, {}):
                lines = self.function_lines(path, func)
                if lines and lines[1] - lines[0] > 0:
                    out.append((path, func, lines[0], lines[1]))
        out.sort(key=lambda f: (-(f[3] - f[2]), f[1]))
        return out


def percent(covered, total):
    return 100.0 * covered / total if total else 0.0


def show(component, name):
    """How a command is run on the command line."""
    if component == BUILTIN:
        return f'nvme {name}'
    return f'nvme {component} {name}'


def report(coverage, commands, callbacks, plugins, only=(), top=5,
           show_all=False, baseline=None):
    """Print the report. Returns the components it reported on."""
    by_component = {}
    for component, name, _ in commands:
        by_component.setdefault(component, []).append(name)

    rows = []
    for component, names in by_component.items():
        if only and component not in only:
            continue
        covered, total = coverage.component_lines(component, plugins)
        if not total:
            continue  # built without coverage, or not built at all
        missing = []
        for name in sorted(names):
            callback = callbacks.get((component, name))
            if callback and not coverage.ran(callback, baseline=baseline):
                missing.append(name)
        if missing or show_all or only:
            rows.append((component, names, missing, covered, total))

    rows.sort(key=lambda r: (-len(r[2]), percent(r[3], r[4]), r[0]))

    for component, names, missing, covered, total in rows:
        whose = ("the built-in commands'" if component == BUILTIN
                 else "the plugin's")
        print(f'{component}: {len(names) - len(missing)} of {len(names)} '
              f'commands run by a test, {percent(covered, total):.0f}% of '
              f'{whose} lines covered')
        if missing:
            print('Commands without a test:')
            for name in missing:
                print(f'  {show(component, name)}')
        funcs = coverage.uncovered_functions(component, plugins)[:top]
        if funcs:
            print('Largest uncovered functions (lines covered / total):')
            for _, name, cov, tot in funcs:
                print(f'  {name}(): {cov} / {tot}')
        print()
    return [row[0] for row in rows]


def main():
    parser = argparse.ArgumentParser(
        description=__doc__.split('\n\n')[0],
        epilog='See Documentation/TESTING.md for the whole process.')
    parser.add_argument('build_dir', nargs='?',
                        help='a build configured with -Db_coverage=true '
                             'whose tests have run')
    parser.add_argument('--json', metavar='REPORT',
                        help="a gcovr JSON report, '-' for stdin, instead "
                             'of running gcovr over the build')
    parser.add_argument('--baseline', metavar='REPORT',
                        help='a gcovr JSON report whose coverage does not '
                             'count, e.g. of a run of the tests which only '
                             "print help (`meson test 'nvme-cli - usage'`)")
    parser.add_argument('--metadata', metavar='FILE',
                        help='the output of `nvme utils '
                             'dump-command-metadata`, instead of running '
                             "the build's binary")
    parser.add_argument('--root', type=Path,
                        help='the source tree (default: the one this '
                             'script is in)')
    parser.add_argument('-c', '--component', action='append', default=[],
                        metavar='NAME',
                        help=f'report only this plugin, or {BUILTIN!r} for '
                             'the built-in commands; may be repeated')
    parser.add_argument('-n', '--top', type=int, default=5, metavar='N',
                        help='show the N largest uncovered functions '
                             '(default: 5, 0 for none)')
    parser.add_argument('-a', '--all', action='store_true',
                        help='report every component, not only those with '
                             'a command no test runs')
    parser.add_argument('--deprecated', action='store_true',
                        help='count the deprecated commands too; they only '
                             'run their replacement')
    args = parser.parse_args()

    if not args.build_dir and not args.json:
        parser.error('pass a build directory, or --json and --metadata')
    if args.json and not (args.metadata or args.build_dir):
        parser.error('--json needs --metadata or a build directory')

    root = (args.root or Path(__file__).resolve().parent.parent).resolve()
    if args.json:
        text = (sys.stdin.read() if args.json == '-'
                else Path(args.json).read_text(encoding='utf-8'))
        data = json.loads(text)
    else:
        data = run_gcovr(root, args.build_dir)
    coverage = Coverage(data)
    if not coverage.lines:
        raise SystemExit('the report holds no coverage data: was the build '
                         'configured with -Db_coverage=true and its tests '
                         'run?')

    baseline = None
    if args.baseline:
        baseline = Coverage(json.loads(
            Path(args.baseline).read_text(encoding='utf-8')))
        if not baseline.lines:
            raise SystemExit('the baseline report holds no coverage data')

    if args.metadata:
        metadata = json.loads(Path(args.metadata).read_text(encoding='utf-8'))
    else:
        metadata = dump_metadata(args.build_dir)
    commands = read_metadata(metadata)
    if not commands:
        raise SystemExit('the metadata holds no commands')

    callbacks, plugins = read_callbacks(root)
    unmapped = sorted(show(component, name)
                      for component, name, _ in commands
                      if (component, name) not in callbacks)
    if not args.deprecated:
        commands = [c for c in commands if not c[2]]

    unknown = [c for c in args.component
               if c not in {component for component, _, _ in commands}]
    if unknown:
        raise SystemExit(f'unknown component(s): {", ".join(unknown)}')

    if not report(coverage, commands, callbacks, plugins,
                  only=args.component, top=args.top, show_all=args.all,
                  baseline=baseline):
        print('every command of every component built here is run by a test')

    # A command whose callback was not found is not reported either way, so
    # say so rather than quietly leave it out of the counts.
    if unmapped:
        print(f'note: no callback found in the sources for {len(unmapped)} '
              f'command(s), left out of the counts:', file=sys.stderr)
        for name in unmapped:
            print(f'  {name}', file=sys.stderr)


if __name__ == '__main__':
    main()
