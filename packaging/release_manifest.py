"""Generate an immutable per-file SHA256 manifest for a clean Windows build."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.app_updates import digest, protected


def generate(root, archive, revision):
    files = []
    for path in sorted(root.rglob('*')):
        if not path.is_file(): continue
        name = path.relative_to(root).as_posix()
        if protected(name): continue
        files.append(dict(path=name, size=path.stat().st_size, sha256=digest(path)))
    manifest = dict(schema=1, platform='windows-x64', revision=revision,
                    archive=archive.name, archive_sha256=digest(archive), prefix=root.name, files=files)
    result = archive.parent/'update-manifest-windows-x64.json'
    result.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    sums = archive.parent/'SHA256SUMS.txt'
    sums.write_text(''.join(f'{digest(p)}  {p.name}\n' for p in (archive, result)), encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    args = parser.parse_args()
    generate(args.root, args.archive, args.revision)
