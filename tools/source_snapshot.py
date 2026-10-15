"""Export tracked emulator source without changing its checkout or index."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile


def allowed(name):
    parts = PurePosixPath(name).parts
    if re.search(r"\.so(?:\.\d+)*$", name.lower()):
        return False
    if PurePosixPath(name).suffix.lower() in {'.dll', '.exe', '.obj', '.pdb', '.lib', '.a', '.so', '.dylib', '.pyc', '.key', '.pem', '.zip', '.7z'}:
        return False
    return not any(p.lower() in {'.git', '.env', 'node_modules', '__pycache__', 'venv', 'dist', 'build', '.next', '.catui-deps'}
                   or p.lower().startswith(('.env.', 'build-')) for p in parts)


def snapshot(repo, destination, additions=()):
    repo, destination = Path(repo).resolve(), Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        env = os.environ.copy()
        env['GIT_INDEX_FILE'] = str(Path(temp) / 'index')

        def git(*args):
            return subprocess.run(['git', '-C', str(repo), *args], env=env,
                                  capture_output=True, check=True, timeout=120).stdout

        head = git('rev-parse', 'HEAD').decode().strip()
        git('read-tree', 'HEAD')
        git('add', '-u')
        additions = list(dict.fromkeys(additions))
        for relative in additions:
            (repo / relative).resolve().relative_to(repo)
        for start in range(0, len(additions), 50):
            names = git('ls-files', '--others', '--exclude-standard', '-z', '--', *additions[start:start + 50]).decode().split('\0')
            names = [name for name in names if name and allowed(name) and Path(name).suffix.lower() != '.zip']
            for start in range(0, len(names), 50):
                git('add', '--', *names[start:start + 50])
        names = git('ls-files', '-z').decode().split('\0')
        excluded = [name for name in names if name and not allowed(name)]
        for start in range(0, len(excluded), 50):
            git('update-index', '--force-remove', '--', *excluded[start:start + 50])
        # Audit source changes, not upstream fixtures, before making them public.
        changes = git('diff', '--cached', '--binary', 'HEAD', '--')
        if re.search(rb'gh[pousr]_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{24,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY', changes):
            raise ValueError('Possible credential in local source changes')
        tree = git('write-tree').decode().strip()
        archive = destination / 'source.tar.gz'
        git('archive', '--format=tar.gz', '--output=' + str(archive), tree)
        if archive.stat().st_size >= 100 * 1024 * 1024:
            raise ValueError('Source archive exceeds GitHub file limit')
        modules = []
        for line in git('ls-tree', '-r', tree).decode().splitlines():
            info, name = line.split('\t', 1)
            if info.startswith('160000 '):
                modules.append({'path': name, 'commit': info.split()[2]})
        data = {'archive': archive.name, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                'size': archive.stat().st_size, 'tree': tree, 'localCommit': head,
                'excludedPaths': excluded, 'submodules': modules}
        (destination / 'snapshot.json').write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
        return data
