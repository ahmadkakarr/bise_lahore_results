"""Readable names and reversible publication of generated report folders."""

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo


class ArtifactDigests:
    """Per-operation hashes, reused only while inode and all change stamps match."""
    def __init__(self):
        self.cache = {}
        self.reads = 0

    @staticmethod
    def stamp(path):
        st = Path(path).stat()
        return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns

    def record_written(self, path, value):
        """Seed only the digest of bytes just written by our own stream writer."""
        self.cache[self.stamp(path)] = value

    def __call__(self, path):
        path = Path(path)
        before = self.stamp(path)
        if before not in self.cache:
            with path.open('rb') as stream:
                value = hashlib.file_digest(stream, 'sha256').hexdigest()
            if self.stamp(path) != before:
                raise OSError('Artifact changed while calculating its hash')
            self.cache[before] = value
            self.reads += 1
        return self.cache[before]


def unique_names(values, fallback="Institution", limit=80, reserved=()):
    names, used = {}, {value.casefold() for value in reserved}
    for value in sorted(values):
        base = re.sub(r'[^\w .()-]+', '-', value).strip(' .-')[:limit].rstrip(' .') or fallback
        if base.upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}:
            base += " Institution"
        candidate, counter = base, 1
        while candidate.casefold() in used:
            counter += 1
            suffix = f" ({counter})"
            candidate = base[:limit - len(suffix)] + suffix
        used.add(candidate.casefold())
        names[value] = candidate
    return names


def file_hashes(root, digest=None):
    digest = digest or ArtifactDigests()
    return {path.relative_to(root).as_posix(): digest(path)
            for path in sorted(root.rglob('*')) if path.is_file()}


def preserve_tree(source, archive_root, category, label):
    """Move a complete directory only after recording its file inventory."""
    digests = ArtifactDigests()
    before = file_hashes(source, digest=digests)
    stamp = datetime.now(ZoneInfo('Asia/Karachi')).strftime('%Y-%m-%d %H-%M-%S-%f')
    entry = archive_root / category / label / stamp
    entry.mkdir(parents=True, exist_ok=False)
    target = entry / 'Files'
    try:
        source.rename(target)
        # Directory rename does not change file contents or their stamps. A
        # mutation still invalidates the cached digest and fails the inventory.
        if file_hashes(target, digest=digests) != before:
            raise RuntimeError('Archived report inventory did not match')
        (entry / 'Inventory.json').write_text(json.dumps({'original_path': str(source.resolve()), 'files': before}, indent=2) + '\n')
        return target
    except BaseException:
        if target.exists() and not source.exists():
            target.rename(source)
        raise


def publish_staging(staging, destination):
    staging.rename(destination)


def publish(staging, destination, archive_root):
    previous = None
    if destination.exists():
        if not (destination / 'Data/manifest.json').is_file():
            raise RuntimeError(f'Report destination contains unmanaged files: {destination}')
        previous = preserve_tree(destination, archive_root, 'Previous reports', destination.name)
    try:
        publish_staging(staging, destination)
    except BaseException:
        if previous is not None and not destination.exists():
            previous.rename(destination)
        raise


def archive_legacy(output, batches, archive_root):
    """Archive only known old batch snapshots; preserve unrecognized folders."""
    for batch in batches:
        legacy = output / batch
        if not re.fullmatch(r'[a-f0-9]{16}', batch) or not legacy.is_dir():
            continue
        for snapshot in sorted(legacy.iterdir()):
            manifest_path = snapshot / 'manifest.json'
            if not snapshot.is_dir() or not re.fullmatch(r'\d{8}T\d+', snapshot.name) or not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text())
            except (ValueError, OSError):
                continue
            if manifest.get('batch') == batch:
                preserve_tree(snapshot, archive_root, 'Legacy output', batch)
        if not any(legacy.iterdir()):
            legacy.rmdir()
