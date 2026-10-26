"""Detached Linux run-end alarm. No browser, network or database access."""

from array import array
from datetime import datetime, timezone
import json
import hashlib
import math
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import wave


_project=hashlib.sha256(str(Path(__file__).resolve().parent.parent).encode()).hexdigest()[:12]
DEFAULT_DIRECTORY=Path(os.environ.get('XDG_RUNTIME_DIR') or tempfile.gettempdir())/f'bise-run-alarms-{os.getuid()}-{_project}'


def now():
    return datetime.now(timezone.utc).isoformat()


def write_state(path, state):
    temporary=path.with_suffix('.tmp')
    try:
        temporary.write_text(json.dumps(state,indent=2)+'\n')
        temporary.replace(path)
        return True
    except OSError:
        # Monitoring metadata must not suppress sound after the watcher armed.
        return False


def players(sound):
    """PipeWire first; no shell commands or changes to the master volume."""
    choices=[('pw-play',['--volume=1.0',str(sound)]),
        ('paplay',['--volume=65536',str(sound)]),
        ('ffplay',['-nodisp','-autoexit','-loglevel','error','-volume','100',str(sound)]),
        ('aplay',['-q',str(sound)])]
    return [[binary,*args] for program,args in choices if (binary:=shutil.which(program))]


def make_sound(path):
    """A local two-tone PCM siren: no downloaded assets or optional codecs."""
    samples=array('h')
    rate=16000
    for frequency in (880,1175,880,1175):
        count=int(rate*.3)
        for index in range(count):
            envelope=min(1,index/160,(count-1-index)/160)
            samples.append(int(24500*envelope*math.sin(2*math.pi*frequency*index/rate)))
    if sys.byteorder!='little':samples.byteswap()
    with wave.open(str(path),'wb') as output:
        output.setnchannels(1);output.setsampwidth(2);output.setframerate(rate)
        output.writeframes(samples.tobytes())


def play_once(command, acknowledged, timeout=5):
    """Bound hung players and allow acknowledgment even during playback."""
    try:
        process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    except OSError:return False
    deadline=time.monotonic()+timeout
    try:
        while process.poll() is None:
            if acknowledged() or time.monotonic()>=deadline:return False
            time.sleep(.05)
        return process.returncode==0
    finally:
        if process.poll() is None:
            process.terminate()
            try:process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                process.kill();process.wait()


def notify(label):
    command=shutil.which('notify-send')
    if command:
        try:subprocess.run([command,'--urgency=critical','--app-name=BISE run alarm','Run stopped',label],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=2,check=False)
        except (OSError,subprocess.TimeoutExpired):pass


def silent_exit(reason):
    """Only explicitly reported success or user interruption disarms the alert."""
    if not isinstance(reason,dict):return False
    if reason.get('event')=='user_interrupted':return True
    code=reason.get('exit_code')
    return reason.get('event')=='command_returned' and type(code) is int and code in (0,130)


def watch(fd, path, label, parent_pid, duration=None):
    path=Path(path);ack=path.with_suffix('.ack');sound=path.with_suffix('.wav')
    state={'id':path.stem,'label':label,'parent_pid':parent_pid,'watcher_pid':os.getpid(),
        'status':'watching','armed_at':now(),'plays':0}
    if not write_state(path,state):return
    buffer=b''
    # A private pipe, rather than repeated PID checks: EOF also detects SIGKILL,
    # a closed terminal and normal exits, without PID-reuse races.
    try:
        while True:
            if ack.exists():
                state.update(status='acknowledged',acknowledged_at=now());write_state(path,state);return
            readable,_,_=select.select([fd],[],[],.2)
            if not readable:continue
            chunk=os.read(fd,4096)
            if not chunk:break
            buffer=(buffer+chunk)[-4096:]
    finally:os.close(fd)
    try:reason=json.loads(buffer.decode()) if buffer else {'event':'process_disappeared'}
    except (ValueError,UnicodeError):reason={'event':'process_disappeared'}
    if silent_exit(reason):
        state.update(status='finished',stopped_at=now(),reason=reason)
        write_state(path,state)
        sound.unlink(missing_ok=True)
        return
    state.update(status='alarming',stopped_at=now(),reason=reason)
    write_state(path,state)
    if ack.exists():
        state.update(status='acknowledged',acknowledged_at=now());write_state(path,state);return
    # Make the tone before arming, while the runtime filesystem is writable.
    if not sound.exists():make_sound(sound)
    notify(label)
    deadline=time.monotonic()+duration if duration is not None else None
    def acknowledged():return ack.exists() or (deadline is not None and time.monotonic()>=deadline)
    try:
        while not acknowledged():
            available=players(sound)
            for command in available:
                if acknowledged():break
                if play_once(command,acknowledged):
                    state.update(backend=Path(command[0]).name,plays=state['plays']+1,last_play_at=now(),audio_error=None)
                    break
            else:state['audio_error']='No working playback backend; retrying (check audio service/device)'
            write_state(path,state)
            # Do not spin if the desktop audio service is temporarily unavailable.
            until=time.monotonic()+(.2 if not state.get('audio_error') else 2)
            while not acknowledged() and time.monotonic()<until:time.sleep(.05)
    finally:
        state.update(status='acknowledged' if ack.exists() else 'test_finished',acknowledged_at=now())
        write_state(path,state);sound.unlink(missing_ok=True)


class RunAlarm:
    def __init__(self,label,directory=DEFAULT_DIRECTORY):
        self.directory=Path(directory);self.label=label;self.fd=None;self.process=None;self.path=None

    def start(self):
        if not players(Path('alarm.wav')):
            raise RuntimeError('No audio player found; install PipeWire/pw-play or use --no-alarm explicitly')
        self.directory.mkdir(parents=True,exist_ok=True)
        self.path=self.directory/(uuid.uuid4().hex+'.json')
        make_sound(self.path.with_suffix('.wav'))
        read_fd,write_fd=os.pipe()
        try:
            with self.path.with_suffix('.log').open('ab') as log:
                self.process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),str(read_fd),
                    str(self.path),self.label,str(os.getpid())],pass_fds=(read_fd,),
                    stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,close_fds=True)
            self.fd=write_fd
        except BaseException:
            os.close(write_fd);raise
        finally:os.close(read_fd)
        deadline=time.monotonic()+3
        while not self.path.exists():
            if self.process.poll() is not None or time.monotonic()>=deadline:
                self.close({'event':'watcher_startup_failed'})
                if self.process.poll() is None:self.process.terminate();self.process.wait(timeout=2)
                raise RuntimeError('Alarm watcher did not start; see '+str(self.path.with_suffix('.log')))
            time.sleep(.02)
        return self

    def close(self, reason):
        if self.fd is not None:
            try:os.write(self.fd,json.dumps(reason).encode()[:4000])
            except OSError:pass
            finally:os.close(self.fd);self.fd=None


def add_arguments(parser,default=True):
    import argparse
    parser.add_argument('--alarm',action=argparse.BooleanOptionalAction,default=default,
        help='Repeated sound on errors or unexpected termination; successful completion and Ctrl+C stay silent. Stop: python -m run_alarm stop')


def guarded(args, action, label):
    if not getattr(args,'alarm',False):return action(args)
    alarm=RunAlarm(label)
    try:alarm.start()
    except (OSError,RuntimeError) as exc:
        print('Alarm unavailable: '+str(exc),file=sys.stderr);return 1
    print('Failure alarm ON; completion and Ctrl+C stay silent. Silence with: '+sys.executable+' -m run_alarm stop',file=sys.stderr,flush=True)
    reason={'event':'unhandled_exit'}
    try:
        code=action(args)
        reason={'event':'command_returned','exit_code':code}
        return code
    except KeyboardInterrupt:
        reason={'event':'user_interrupted'}
        raise
    except SystemExit as exc:
        code=0 if exc.code is None else int(exc.code) if isinstance(exc.code,int) else 1
        reason={'event':'command_returned','exit_code':code}
        raise
    except BaseException as exc:
        reason={'event':'unhandled_exit','exception':type(exc).__name__}
        raise
    finally:alarm.close(reason)


def acknowledge(directory=DEFAULT_DIRECTORY):
    count=0
    for path in Path(directory).glob('*.json'):
        try:state=json.loads(path.read_text())
        except (ValueError,OSError):continue
        if state.get('status')=='alarming' and not path.with_suffix('.ack').exists():
            path.with_suffix('.ack').touch(exist_ok=True);count+=1
    return count


if __name__=='__main__':
    # SIGINT/SIGHUP from the original terminal must not disable the watcher.
    signal.signal(signal.SIGHUP,signal.SIG_IGN)
    signal.signal(signal.SIGINT,signal.SIG_IGN)
    watch(int(sys.argv[1]),sys.argv[2],sys.argv[3],int(sys.argv[4]))
