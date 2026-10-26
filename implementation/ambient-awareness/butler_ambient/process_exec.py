"""Minimal isolated exec stage. Never import Butler, numpy or other models."""
import os
import resource
import sys


def main():
    cwd, session, *command = sys.argv[1:]
    try:
        if session == '1': os.setsid()
        if cwd: os.chdir(cwd)
        maximum = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
        if maximum == resource.RLIM_INFINITY: maximum = os.sysconf('SC_OPEN_MAX')
        os.closerange(3, maximum)
        os.execvpe(command[0], command, os.environ)
    except OSError:
        # No command arguments, credentials or private paths in diagnostics.
        os.write(2, b'BUTLER_PROCESS exec-unavailable\n')
        return 127


if __name__ == '__main__':
    raise SystemExit(main())
