"""Copy a built federation graph and record exactly the published assets."""
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def copy():
    source = ROOT / 'frontend/dist/assets'
    target = ROOT / 'plugins.v2/p115tool/dist/assets'
    entry = source / 'remoteEntry.js'
    if not entry.is_file():
        raise ValueError('Build the frontend first')
    pending, payload = ['remoteEntry.js'], {}
    while pending:
        name = pending.pop()
        if name in payload:
            continue
        if '/' in name or '\\' in name or name.startswith('.'):
            raise ValueError('Invalid federation asset')
        content = (source / name).read_text('utf-8')
        if name == 'remoteEntry.js':
            # OriginJS/Vite emits ./assets/ imports in an entry already hosted
            # under assets/. MoviePilot serves that directory directly; rebase
            # the entry to its siblings instead of requiring nested duplicates.
            content = content.replace('"./assets/', '"./')
        if name == 'remoteEntry.js':
            # OriginJS/Vite emits ./assets/ imports in an entry already hosted
            # under assets/. MoviePilot serves that directory directly; rebase
            # the entry to its siblings instead of requiring nested duplicates.
            content = content.replace('"./assets/', '"./')
        if name.endswith('.css') and re.search(r'(?m)(?:^|})\s*\.v-[\w-]+\s*[{,]', content):
            raise ValueError('Unscoped host component CSS is forbidden')
        payload[name] = content.encode('utf-8')
        pending.extend(re.findall(r'["\'](?:\./)?([^/"\']+\.(?:js|css))["\']', content))
    telegram = ROOT / 'frontend/telegram'
    if telegram.is_dir():
        # The later repository adds Telegram as wrappers over the original Vue
        # components. Rebind those source wrappers to this freshly built graph.
        for component in ('Page', 'Config'):
            baseline = next(name for name in payload if name.startswith('__federation_expose_' + component + '-'))
            wrapper = telegram / (component.lower() + '.js')
            content = re.sub(r'__federation_expose_' + component + r'-[^\x27\"]+\.js', baseline, wrapper.read_text('utf-8'))
            wrapper.write_bytes(content.encode('utf-8'))
        for name, output in (('panel.js','telegram-panel.js'), ('config.js','telegram-config.js'),
                             ('navigation.js','telegram-navigation.js'), ('page.js','telegram-page.js'),
                             ('telegram.css','telegram.css'), ('remoteEntry.js','remoteEntry.js')):
            payload[output] = (telegram / name).read_text('utf-8').encode('utf-8')
    target.mkdir(parents=True, exist_ok=True)
    for name, content in payload.items():
        (target / name).write_bytes(content)
    sources = [ROOT / 'frontend/package.json', ROOT / 'frontend/package-lock.json',
               ROOT / 'frontend/index.html', ROOT / 'frontend/vite.config.js', *sorted((ROOT / 'frontend/src').glob('*'))]
    if telegram.is_dir(): sources += sorted(telegram.glob('*'))
    manifest = {'files': {name: hashlib.sha256(content).hexdigest() for name, content in sorted(payload.items())},
                'sources': {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}}
    (ROOT / 'plugins.v2/p115tool/dist/manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    print(f'Copied {len(payload)} federation assets')


if __name__ == '__main__':
    copy()
