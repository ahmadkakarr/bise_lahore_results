"""Resume a pinned, confirmed input without rebuilding completed normalization."""

from pathlib import Path

from bise_normalize import VERSION
from youthgames_shared.names import NAME_POLICY
from youthgames_register.config import load, resolved, sha, signature
from youthgames_register.store import opened, now
from youthgames_register.browser_recovery import recover_browser_jobs
from . import app


def context_hash(profile, paths, settings):
    # Hash small configuration files, not the growing BISE database. Refreshing
    # newly fetched source students is an explicit --refresh-input operation.
    files={str(Path(profile).resolve()):sha(Path(profile).read_bytes())}
    for key in ('normalization_base','normalization_profile','registration_settings','registration_profile','catalog'):
        path=paths[key]
        files[str(path)]=sha(path.read_bytes()) if path.is_file() else None
    pinned={}
    for key in ('csv','manifest','provenance'):
        path=resolved(paths['registration_profile'],settings['input'][key])
        if not path.is_file():return None
        stat=path.stat()
        pinned[str(path)]=[stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns]
    from .participation import current
    participation=current(paths['allocation_database'],app.load(profile)['scheme'])
    return signature({'participation':participation,'policy':'pinned-resume-v1','normalizer':VERSION,'names':NAME_POLICY,'files':files,'inputs':pinned})


def prepare(profile, refresh=False, emit=None):
    config=app.load(profile);paths=app.paths(config,profile)
    emit=emit or (lambda *args,**kwargs:None)
    reused=False;count=0
    if paths['registration_profile'].exists():
        settings=load(paths['registration_profile'])
        with opened(paths['registration_database']) as db:
            if db.execute('SELECT 1 FROM profiles WHERE name=?',(settings['profile'],)).fetchone():
                recovery=recover_browser_jobs(db,settings)
                count=sum(len(value) for key,value in recovery.items() if key.startswith('recovered_'))
            cached=db.execute('SELECT * FROM prepared_inputs WHERE profile=?',(settings['profile'],)).fetchone()
            current=context_hash(profile,paths,settings)
            reused=bool(not refresh and not count and current and cached
                and cached['context_hash']==current and cached['input_hash']==settings['input']['expected_csv_sha256'])
    if reused:
        emit('stage',message='Resuming pinned input; confirmed applicants skipped via indexed SQLite ledger')
    else:
        if count:emit('stage',message=f'Recovered {count} receipt-proven cases; refreshing eligible input')
        emit('stage',message='Refreshing eligible students from SQLite; later reruns reuse this pinned input')
        app.sync(profile)
        settings=load(paths['registration_profile'])
        current=context_hash(profile,paths,settings)
        if current is None:raise ValueError('Refreshed registration input is incomplete')
        with opened(paths['registration_database']) as db:
            with db:db.execute('INSERT OR REPLACE INTO prepared_inputs VALUES (?,?,?,?)',
                (settings['profile'],current,settings['input']['expected_csv_sha256'],now()))
    return {'input_reused':reused,'recovered_before_refresh':count}
