"""Build and verify version-matched native and repository ZIPs from a whitelist."""
from hashlib import sha256
import json
from pathlib import Path
import re
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / 'plugins.v2' / 'p115tool'


def build():
    version = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
    assert re.search(r"plugin_version='([^']+)'", (PLUGIN / '__init__.py').read_text())[1] == version
    assert json.loads((ROOT / 'package.v2.json').read_text())['P115Tool']['version'] == version
    manifest = json.loads((PLUGIN / 'dist' / 'manifest.json').read_text())
    for name, digest in manifest['files'].items():
        assert sha256((PLUGIN / 'dist' / 'assets' / name).read_bytes()).hexdigest() == digest, name
    for name, digest in manifest['sources'].items():
        assert sha256((ROOT / name).read_bytes()).hexdigest() == digest, name
    def files(directory):
        for path in sorted(directory.rglob('*')):
            if path.is_symlink(): raise RuntimeError('Refusing symlink in package')
            if not path.is_file() or '__pycache__' in path.parts or path.suffix in ('.pyc', '.pyo'): continue
            yield path
    plugin_files = list(files(PLUGIN))
    assert all(p.suffix in ('.py','.txt','.js','.css','.json') for p in plugin_files)
    repository_files = list(plugin_files)
    for name in ('README.md','AGENTS.md','package.v2.json','pyproject.toml','.gitignore'):
        repository_files.append(ROOT / name)
    for name in ('docs','frontend','tools','tests'): repository_files.extend(files(ROOT / name))
    output = ROOT / 'dist' / ('moviepilot-' + version)
    output.mkdir(parents=True, exist_ok=True)
    for name, paths, base in ((f'p115tool_v{version}.zip',plugin_files,PLUGIN),
                              (f'p115tool_repository_v{version}.zip',repository_files,ROOT)):
        target = output / name
        with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
            for path in paths: archive.write(path,path.relative_to(base).as_posix())
        with zipfile.ZipFile(target) as archive:
            assert archive.testzip() is None
            for path in paths: assert archive.read(path.relative_to(base).as_posix()) == path.read_bytes()
        print(f'PASS {target.relative_to(ROOT)} ({len(paths)} files)')


if __name__ == '__main__': build()
