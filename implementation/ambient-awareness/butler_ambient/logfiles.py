"""A size cap for the logs that launchd holds open (BUG-027).

The dialogue Ollama job writes `dialogue-ollama.log` and `dialogue-ollama-error.log` through files launchd opened for it.
launchd cannot rotate them and the job cannot be asked to reopen them, so a rename would leave it writing to the renamed
file. They are copied to a numbered generation and then emptied in place (copy-truncate). launchd opens these files for
appending, so the job's next write lands at the start of the emptied file with no gap.

The few lines written between the copy and the emptying are lost; that is the price of not touching the job. The copy is
made first and the file is only emptied once the copy is complete, so a full disk never costs the log.
"""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

LAUNCHD_LOGS = ("dialogue-ollama.log", "dialogue-ollama-error.log")
MAX_BYTES = 4 * 1024 * 1024        # worst case per log: the live file plus KEEP generations, 12 MiB
KEEP = 2
CHECK_SECONDS = 600


def rotate(path, max_bytes=None, keep=KEEP):
    """Rotate one log if it is over the cap; True when it was."""
    path = Path(path)
    limit = MAX_BYTES if max_bytes is None else max_bytes
    try:
        if path.stat().st_size <= limit: return False
    except FileNotFoundError: return False
    generation = lambda n: path.with_name(f"{path.name}.{n}")
    partial = path.with_name(path.name + ".partial")
    try:
        shutil.copyfile(path, partial)
        partial.chmod(stat.S_IMODE(path.stat().st_mode))
        for n in range(keep, 1, -1):
            if generation(n - 1).exists(): os.replace(generation(n - 1), generation(n))
        os.replace(partial, generation(1))
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    os.truncate(path, 0)
    return True


def rotate_all(directory):
    """Rotate the launchd-owned logs in a directory; the names of the ones that were rotated."""
    directory = Path(directory)
    return [name for name in LAUNCHD_LOGS if rotate(directory / name)]
