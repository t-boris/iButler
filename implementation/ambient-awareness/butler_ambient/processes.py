"""Fork-free service subprocesses, including cwd/session launches on macOS.

Native numerical libraries may deadlock in atfork while holding Python's GIL.
The tiny exec helper has no model imports and applies options that would make
CPython fall back to fork. No shell or external request broker is involved.
"""
from __future__ import annotations

import os
import shutil
import subprocess as _subprocess
import sys
import time
from pathlib import Path
from subprocess import (PIPE, STDOUT, DEVNULL, CalledProcessError, CompletedProcess,
                        SubprocessError, TimeoutExpired)


def module_environment(*roots, env=None):
    """Import our worker from a checkout or the selected interpreter's install.

    Putting site-packages on PYTHONPATH moves third-party backports ahead of the
    standard library. Installed packages already have the right search order.
    """
    result = dict(os.environ if env is None else env)
    paths = [os.fspath(root) for root in roots]
    package_root = Path(__file__).resolve().parent.parent
    if package_root.name not in {'site-packages', 'dist-packages'}:
        paths.insert(0, str(package_root))
    if paths: result['PYTHONPATH'] = os.pathsep.join(dict.fromkeys(paths))
    else: result.pop('PYTHONPATH', None)
    return result


class Popen(_subprocess.Popen):
    def __init__(self, args, **kwargs):
        if not getattr(_subprocess, '_USE_POSIX_SPAWN', False):
            raise RuntimeError('fork-free process launch unavailable')
        if (not isinstance(args, (list, tuple)) or not args or kwargs.get('shell')
                or kwargs.get('preexec_fn') is not None or kwargs.get('pass_fds')
                or kwargs.get('executable') is not None
                or kwargs.get('process_group', -1) not in (-1, None)
                or any(kwargs.get(k) is not None for k in ('user','group','extra_groups'))
                or kwargs.get('umask', -1) != -1):
            raise ValueError('unsupported fork-free launch options')
        original = list(args)
        environment = kwargs.get('env')
        executable = shutil.which(os.fspath(args[0]), path=environment.get('PATH') if environment is not None else None)
        if executable is None: raise FileNotFoundError('child executable unavailable')
        cwd = kwargs.pop('cwd', None)
        if cwd is not None and not Path(cwd).is_dir(): raise FileNotFoundError('child directory unavailable')
        session = kwargs.pop('start_new_session', False)
        # Every inherited nonstandard descriptor is closed before exec in the
        # helper, including intentionally inheritable descriptors from libraries.
        kwargs['close_fds'] = False
        command = [sys.executable, '-I', '-S', str(Path(__file__).with_name('process_exec.py')),
                   str(Path(cwd).absolute()) if cwd is not None else '', '1' if session else '0',
                   str(Path(executable).absolute()), *map(os.fspath, args[1:])]
        super().__init__(command, **kwargs)
        self.args = original
        if session:
            # Callers cancel only this new process group. Wait until setsid has
            # happened so an immediate cancel cannot miss the new group.
            deadline = time.monotonic()+3
            while self.poll() is None:
                try:
                    if os.getpgid(self.pid) == self.pid: break
                except ProcessLookupError: break
                if time.monotonic() >= deadline:
                    self.kill(); self.wait()
                    raise RuntimeError('child session unavailable')
                time.sleep(.002)


def run(args, *, input=None, capture_output=False, timeout=None, check=False, **kwargs):
    if input is not None:
        if kwargs.get('stdin') is not None: raise ValueError('stdin and input are exclusive')
        kwargs['stdin'] = PIPE
    if capture_output:
        if kwargs.get('stdout') is not None or kwargs.get('stderr') is not None:
            raise ValueError('capture_output and stdout/stderr are exclusive')
        kwargs.update(stdout=PIPE, stderr=PIPE)
    with Popen(args, **kwargs) as process:
        try: stdout, stderr = process.communicate(input, timeout=timeout)
        except BaseException:
            process.kill(); process.communicate()
            raise
        result = CompletedProcess(args, process.returncode, stdout, stderr)
    if check: result.check_returncode()
    return result
