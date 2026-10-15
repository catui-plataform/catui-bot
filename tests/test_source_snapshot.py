from pathlib import Path
import hashlib
import json
import subprocess
import tarfile
import tempfile
import unittest
from tools.source_snapshot import snapshot


class SourceSnapshotTests(unittest.TestCase):
    def test_export_preserves_checkout_and_excludes_secrets_and_builds(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, output = root / 'repo', root / 'export'
            repo.mkdir()

            def git(*args):
                return subprocess.run(['git', '-C', str(repo), *args], check=True,
                                      capture_output=True, timeout=15).stdout

            git('init')
            git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@example.test')
            (repo / 'core.cpp').write_text('original\n')
            (repo / '.env').write_text('PRIVATE=fixture\n')
            (repo / 'build').mkdir()
            (repo / 'build/temporary.cpp').write_text('generated\n')
            (repo / 'dependency.lib').write_bytes(b'compiled')
            git('add', '.')
            git('commit', '-m', 'Initial fixture')
            (repo / 'core.cpp').write_text('modified\n')
            (repo / 'adapter').mkdir()
            (repo / 'adapter/proxy.cpp').write_text('adapter\n')
            (repo / 'adapter/staged.cpp').write_text('staged integration\n')
            git('add', 'adapter/staged.cpp')
            (repo / 'adapter/.env.production').write_text('PRIVATE=fixture\n')
            (repo / 'adapter/generated.dll').write_bytes(b'compiled')
            before = git('status', '--porcelain')
            index = git('write-tree')
            data = snapshot(repo, output, ['adapter'])
            self.assertEqual(before, git('status', '--porcelain'))
            self.assertEqual(index, git('write-tree'))
            with tarfile.open(output / 'source.tar.gz') as archive:
                files = {item.name for item in archive.getmembers() if item.isfile()}
                self.assertEqual(files, {'core.cpp', 'adapter/proxy.cpp', 'adapter/staged.cpp'})
                self.assertEqual(archive.extractfile('core.cpp').read().replace(b'\r\n', b'\n'), b'modified\n')
            self.assertEqual(data['sha256'], hashlib.sha256((output / 'source.tar.gz').read_bytes()).hexdigest())
            self.assertEqual(json.loads((output / 'snapshot.json').read_text()), data)
