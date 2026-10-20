"""Build the native-ES-module Telegram extension over the shipped Vue baseline.

The repository lacks the original Vue sources. Keep the existing compiled
modules intact and wrap their public components using the same shared Vue.
No node dependency, generated-code editing or original source reconstruction.
"""
from hashlib import sha256
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'frontend' / 'telegram'
DIST = ROOT / 'plugins.v2' / 'p115tool' / 'dist'


def build():
    manifest = json.loads((DIST / 'manifest.json').read_text())
    for source, target in (('panel.js', 'telegram-panel.js'), ('config.js', 'telegram-config.js'),
                           ('navigation.js', 'telegram-navigation.js'),
                           ('page.js', 'telegram-page.js'), ('telegram.css', 'telegram.css'),
                           ('remoteEntry.js', 'remoteEntry.js')):
        (DIST / 'assets' / target).write_bytes((SOURCE / source).read_bytes())
    manifest['files'] = {path.name: sha256(path.read_bytes()).hexdigest()
                         for path in sorted((DIST / 'assets').iterdir()) if path.is_file()}
    if 'baseline_sources' not in manifest:
        manifest['baseline_sources'] = manifest.pop('sources', {})
    manifest['sources'] = {str(path.relative_to(ROOT)): sha256(path.read_bytes()).hexdigest()
                          for path in sorted(SOURCE.iterdir()) if path.is_file()}
    manifest['sources']['tools/build_telegram_ui.py'] = sha256(Path(__file__).read_bytes()).hexdigest()
    (DIST / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')


if __name__ == '__main__': build()
