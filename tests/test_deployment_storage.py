"""Synthetic deployment records only; no real disks, Docker data or keys touched."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / 'deploy'
spec = importlib.util.spec_from_file_location('storage_volumes', DEPLOY / 'verify-storage-volumes.py')
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)


def docker_records(monkeypatch, records):
    def run(command, **kwargs):
        if command[1:3] == ['volume', 'ls']:
            return subprocess.CompletedProcess(command, 0, '\n'.join(records))
        return subprocess.CompletedProcess(command, 0, json.dumps([records[command[-1]]]))
    monkeypatch.setattr(storage.subprocess, 'run', run)


def approved_records():
    return {'open-video-downloader_' + name: {
        'Driver': 'local', 'Options': {'type': 'none', 'o': 'bind', 'device': path}
    } for name, path in storage.EXPECTED.items()}


def test_all_runtime_data_on_approved_disk(monkeypatch):
    docker_records(monkeypatch, approved_records())
    storage.verify(require_present=True)


@pytest.mark.parametrize('name', ['media', 'library', 'state', 'config'])
def test_existing_boot_disk_volume_stops_deployment_without_migration(monkeypatch, name):
    records = approved_records()
    records['open-video-downloader_' + name]['Options'] = None
    docker_records(monkeypatch, records)
    with pytest.raises(ValueError, match='Preserve it'):
        storage.verify()


def test_missing_volumes_allowed_only_before_first_start(monkeypatch):
    docker_records(monkeypatch, {})
    storage.verify()
    with pytest.raises(ValueError, match='missing'):
        storage.verify(require_present=True)


def test_docker_failure_is_not_mistaken_for_empty_volume_inventory(monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])
    monkeypatch.setattr(storage.subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        storage.verify()


def test_missing_data_mount_refuses_before_any_disk_or_key_write(tmp_path):
    for name, body in {
        'uname': 'if [ "$1" = -s ]; then echo Linux; else echo aarch64; fi',
        'id': 'echo 0',
        'mountpoint': 'exit 1',
        'install': 'echo unexpected-write >&2; exit 99',
    }.items():
        command = tmp_path / name
        command.write_text('#!/bin/sh\n' + body + '\n')
        command.chmod(0o700)
    result = subprocess.run(['/bin/bash', str(DEPLOY / 'prepare-cloud-storage.sh')],
                            env=dict(os.environ, PATH=str(tmp_path) + ':' + os.environ['PATH']),
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert 'No boot-disk fallback' in result.stderr
    assert 'unexpected-write' not in result.stderr
