"""Cooperative collection limits, scoped to this process and its OCR children."""
import asyncio
import os
from pathlib import Path


def available_memory_mb(path=Path('/proc/meminfo'), cgroup=Path('/sys/fs/cgroup')):
    try:
        fields=dict(line.split(':',1) for line in path.read_text().splitlines())
        available=int(fields['MemAvailable'].split()[0])//1024
        try:
            maximum=(cgroup/'memory.max').read_text().strip()
            if maximum!='max':
                current=int((cgroup/'memory.current').read_text())
                available=min(available,max(0,int(maximum)-current)//1024**2)
        except (OSError,ValueError):pass
        return available
    except (OSError,KeyError,ValueError):
        return None  # Unsupported host: never pretend an inventory error is OOM.


def configure_ocr_children():
    # Environment inheritance affects only this collector's future children.
    # Prevent workers x Tesseract OpenMP threads from oversubscribing the CPU.
    os.environ['OMP_THREAD_LIMIT']='1'
    os.environ['OMP_NUM_THREADS']='1'


class CollectionResources:
    def __init__(self,ocr_workers=2,minimum_memory_mb=1536,*,emit=None,memory=available_memory_mb,poll_seconds=2):
        if type(ocr_workers) is not int or not 1<=ocr_workers<=8:
            raise ValueError('OCR workers must be between 1 and 8')
        if type(minimum_memory_mb) is not int or minimum_memory_mb<0:
            raise ValueError('Minimum available memory must be nonnegative MiB')
        self.ocr=asyncio.Semaphore(ocr_workers)
        self.minimum=minimum_memory_mb;self.memory=memory;self.poll_seconds=poll_seconds
        self.emit=emit or (lambda **values:None)
        self.waiting=False

    async def wait_for_memory(self,needed=lambda:True):
        while needed():
            available=self.memory()
            if available is None or available>=self.minimum:
                if self.waiting:
                    self.waiting=False
                    self.emit(phase='resource_resumed',available_memory_mb=available)
                return True
            if not self.waiting:
                self.waiting=True
                self.emit(phase='resource_wait',reason='low_available_memory',
                    available_memory_mb=available,minimum_available_memory_mb=self.minimum,
                    pending_rolls_preserved=True)
            await asyncio.sleep(self.poll_seconds)
        return False
