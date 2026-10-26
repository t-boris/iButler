"""Real process launches must stay fork-free beside active numerical workers."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from butler_ambient import processes


class ProcessTests(unittest.TestCase):
    def test_argv_environment_streams_and_cwd_are_preserved_without_native_fork(self):
        argument='Русский текст\n $()` ; " дословно'
        code='import sys,os,json;print(json.dumps([sys.argv[1],os.environ["BUTLER_PROCESS_TEST"],os.getcwd(),sys.stdin.read()]))'
        with tempfile.TemporaryDirectory() as root, patch('subprocess._fork_exec',side_effect=AssertionError('native fork forbidden')):
            result=processes.run([sys.executable,'-c',code,argument],input='полная фраза',text=True,
                capture_output=True,check=True,timeout=5,cwd=root,env={**os.environ,'BUTLER_PROCESS_TEST':'test-only'})
            self.assertEqual(json.loads(result.stdout),[argument,'test-only',str(Path(root).resolve()),'полная фраза'])
            self.assertEqual(result.args[-1],argument)

    def test_even_deliberately_inheritable_nonstandard_descriptor_is_closed_before_exec(self):
        with tempfile.TemporaryFile() as private:
            os.set_inheritable(private.fileno(),True)
            code='import os,sys\ntry:os.fstat(int(sys.argv[1]));print("leaked")\nexcept OSError:print("closed")'
            with patch('subprocess._fork_exec',side_effect=AssertionError('native fork forbidden')):
                result=processes.run([sys.executable,'-c',code,str(private.fileno())],capture_output=True,text=True,check=True,timeout=5)
            self.assertEqual(result.stdout.strip(),'closed')

    def test_new_session_is_ready_for_immediate_group_cancellation(self):
        with patch('subprocess._fork_exec',side_effect=AssertionError('native fork forbidden')):
            child=processes.Popen([sys.executable,'-c','import time;time.sleep(30)'],
                start_new_session=True,stdin=processes.DEVNULL,stdout=processes.DEVNULL,stderr=processes.DEVNULL)
            try:
                self.assertEqual(os.getpgid(child.pid),child.pid)
                os.killpg(child.pid,signal.SIGTERM)
                self.assertEqual(child.wait(timeout=3),-signal.SIGTERM)
            finally:
                if child.poll() is None:child.kill();child.wait()

    def test_timeout_check_and_invalid_modes_do_not_fall_back_to_fork(self):
        with patch('subprocess._fork_exec',side_effect=AssertionError('native fork forbidden')):
            with self.assertRaises(processes.TimeoutExpired):
                processes.run([sys.executable,'-c','import time;time.sleep(30)'],timeout=.2)
            with self.assertRaises(processes.CalledProcessError):
                processes.run([sys.executable,'-c','raise SystemExit(7)'],check=True,timeout=5)
            for options in ({'shell':True},{'preexec_fn':lambda:None},{'pass_fds':(3,)},{'process_group':0}):
                with self.assertRaises(ValueError):processes.Popen([sys.executable,'-c','pass'],**options)

    def test_real_numeric_work_and_repeated_background_child_launches_remain_responsive(self):
        import numpy as np
        ready=threading.Event();stop=threading.Event();errors=[]
        def numeric():
            try:
                a=np.ones((128,128));ready.set()
                while not stop.is_set():np.dot(a,a);stop.wait(.001)
            except BaseException as e:errors.append(type(e).__name__)
        thread=threading.Thread(target=numeric,daemon=True);thread.start()
        self.assertTrue(ready.wait(3))
        try:
            with tempfile.TemporaryDirectory() as root, patch('subprocess._fork_exec',side_effect=AssertionError('native fork forbidden')):
                for i in range(16):
                    result=processes.run([sys.executable,'-c','print("ready")'],cwd=root if i%2 else None,
                        start_new_session=bool(i%3),capture_output=True,text=True,timeout=5,check=True)
                    self.assertEqual(result.stdout.strip(),'ready')
                    self.assertTrue(thread.is_alive())
        finally:stop.set();thread.join(3)
        self.assertFalse(errors)


if __name__=='__main__':unittest.main()
