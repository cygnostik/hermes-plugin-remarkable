"""Human-run setup, with masked enrollment and an isolated rendering runtime."""
from __future__ import annotations

import argparse
import getpass
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import webbrowser

if __package__:
    from . import tools
else:
    _spec = importlib.util.spec_from_file_location('remarkable_setup_tools', Path(__file__).with_name('tools.py'))
    tools = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(tools)


def setup_parser(parser):
    subs = parser.add_subparsers(dest='remarkable_command', required=True)
    enroll = subs.add_parser('enroll', help='Pair using a masked local code prompt (never chat)')
    enroll.add_argument('--token-file', help='Explicit durable device-token destination')
    enroll.add_argument('--no-browser', action='store_true')
    subs.add_parser('status', help='Check cloud enrollment and access')
    subs.add_parser('clear-session', help='Forget the cached session only; preserve device enrollment')
    subs.add_parser('setup-cloud', help='Install locked Node dependencies (requires Node 22+ and npm)')
    subs.add_parser('setup-renderer', help='Install pinned rendering dependencies into a private plugin venv')


def run(args, config=None):
    config = dict(config or {})
    command = args.remarkable_command
    if command == 'clear-session':
        try:
            Path(str(tools._resolve_token_file(config)) + '.session.json').unlink(missing_ok=True)
        except OSError:
            print('Could not remove the cached session. Device enrollment was not changed.', file=sys.stderr)
            return 1
        print('Cached session cleared. Device enrollment preserved; the next cloud operation obtains a fresh session.')
        return 0
    if command == 'setup-cloud':
        npm = shutil.which('npm')
        if not npm:
            print('Install Node.js 22+ with npm, then rerun setup-cloud.', file=sys.stderr)
            return 2
        try:
            result = subprocess.run([npm, 'ci', '--ignore-scripts', '--no-audit', '--no-fund'],
                                    cwd=str(Path(__file__).parent / 'sidecar'), check=False)
            return result.returncode
        except OSError:
            print('npm could not start. Verify your Node.js installation.', file=sys.stderr)
            return 2
    if command == 'enroll':
        if not sys.stdin.isatty():
            print('Enrollment requires your interactive terminal. Do not pass the code via chat, arguments, or piped stdin.', file=sys.stderr)
            return 2
        if args.token_file:
            config['token_file'] = args.token_file
        url = 'https://my.remarkable.com/device/desktop/connect'
        print('Pair this device at: ' + url)
        if not args.no_browser:
            webbrowser.open(url)
        code = getpass.getpass('One-time pairing code (hidden): ').strip()
        if len(code) != 8 or not code.isascii() or not code.isalnum():
            print('Expected the eight-character pairing code. Nothing was changed.', file=sys.stderr)
            return 2
        response = tools._call_sidecar('enroll', {'code': code}, config)
        code = None
        if not response.get('ok'):
            print('Pairing failed (' + str(response.get('code', 'ERROR')) + '). Get a fresh code and retry; check Node and network access.', file=sys.stderr)
            return 1
        print('Enrolled. Device token saved outside the plugin installation. Revoke it from your reMarkable account when needed.')
        return 0
    if command == 'status':
        result = json.loads(tools.remarkable_status({}, config=config))
        print(json.dumps(result, indent=2))
        return 1 if result.get('error') or result.get('cloud') != 'ok' else 0
    if command == 'setup-renderer':
        uv = shutil.which('uv')
        if not uv:
            print('uv is required for isolated renderer setup. Install uv, then retry.', file=sys.stderr)
            return 2
        env = tools._state_dir(config) / 'renderer-venv'
        python = env / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
        commands = []
        if not python.is_file():
            commands.append([uv, 'venv', '--python', '3.11', str(env)])
        commands.append([uv, 'pip', 'install', '--python', str(python), '--only-binary', ':all:', '-r', str(Path(__file__).with_name('requirements-render.txt'))])
        for cmd in commands:
            try:
                proc = subprocess.run(cmd, check=False)
            except OSError:
                print('Renderer setup could not start. Verify uv and directory permissions; existing cloud tools are unaffected.', file=sys.stderr)
                return 2
            if proc.returncode:
                print('Renderer setup failed. Existing cloud tools are unaffected.', file=sys.stderr)
                return proc.returncode
        print('Renderer installed: ' + str(python))
        return 0
    return 2


def main(argv=None):
    parser = argparse.ArgumentParser(description='reMarkable plugin setup')
    setup_parser(parser)
    return run(parser.parse_args(argv))


if __name__ == '__main__':
    raise SystemExit(main())
