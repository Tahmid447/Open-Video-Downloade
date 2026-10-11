"""Refuse legacy boot-disk volumes; never migrate, delete or print their data."""
import json
import subprocess
import sys

EXPECTED = {
    'media': '/srv/ovd-data/downloads',
    'library': '/srv/ovd-data/library',
    'state': '/srv/ovd-data/state',
    'config': '/srv/ovd-data/config',
}


def validate(volume, path):
    options = volume.get('Options') or {}
    if (volume.get('Driver') != 'local' or options.get('type') != 'none'
            or options.get('o') != 'bind' or options.get('device') != path):
        raise ValueError('Existing runtime volume is outside the approved data disk. '
                         'Preserve it and complete a reviewed migration before deployment.')


def verify(require_present=False):
    names = subprocess.run(['docker', 'volume', 'ls', '--format', '{{.Name}}'],
                           check=True, capture_output=True, text=True).stdout.splitlines()
    for name, path in EXPECTED.items():
        identity = 'open-video-downloader_' + name
        if identity not in names:
            if require_present:
                raise ValueError('An expected runtime volume is missing.')
            continue
        raw = subprocess.run(['docker', 'volume', 'inspect', identity],
                             check=True, capture_output=True, text=True).stdout
        volumes = json.loads(raw)
        if len(volumes) != 1:
            raise ValueError('Unexpected runtime volume inspection result.')
        validate(volumes[0], path)


if __name__ == '__main__':
    try:
        if sys.argv[1:] not in ([], ['--require-present']):
            raise ValueError('Usage: verify-storage-volumes.py [--require-present]')
        verify(bool(sys.argv[1:]))
    except (subprocess.SubprocessError, json.JSONDecodeError):
        sys.exit('Runtime volume inspection failed; deployment stopped.')
    except ValueError as exc:
        sys.exit(str(exc))
    print('Runtime volumes use the approved data disk; existing data was preserved.')
