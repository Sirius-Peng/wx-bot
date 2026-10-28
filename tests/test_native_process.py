"""Owned native gateway containment: no shell, adoption, global process-name kill."""
from __future__ import annotations
import asyncio
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from src.core import native_process as native
from src.core.native_process import OwnedProcess, _WindowsChild


class FakeWinAPI:
    def __init__(self, fail=''):
        self.calls=[];self.fail=fail;self.count=1;self.code=None
    def create_job(self):self.calls.append('create_job');return 'job'
    def create_process(self, argv, cwd, env, log_fd, input_fd):
        self.calls.append('create_suspended')
        self.inheritance=(os.get_inheritable(log_fd),os.get_inheritable(input_fd))
        self.argv=argv;self.cwd=cwd;self.env=env
        if self.fail=='create':raise OSError('create failed')
        return 'process','thread',1234
    def assign(self, job, process):
        self.calls.append('assign')
        if self.fail=='assign':raise OSError('assign failed')
    def resume(self, thread):
        self.calls.append('resume')
        if self.fail=='resume':raise OSError('resume failed')
    def poll(self, process):self.calls.append('poll');return self.code
    def active(self, job):self.calls.append('active');return self.count
    def terminate_process(self, process):self.calls.append('terminate_process');self.count=0;self.code=1
    def terminate_job(self, job):self.calls.append('terminate_job');self.count=0;self.code=1
    def close_handle(self, handle):self.calls.append('close_'+handle)
    def wait(self, process):self.calls.append('wait')


class WindowsBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.fd=os.open(self.root/'log',os.O_CREAT|os.O_WRONLY,0o600)
    def tearDown(self):os.close(self.fd);self.temp.cleanup()
    def child(self,api):return _WindowsChild([str(Path(sys.executable).resolve())],self.root,{'A':'value'},self.fd,api=api)
    def test_suspend_assign_resume_order_and_stdio_handles_only(self):
        api=FakeWinAPI();child=self.child(api)
        self.assertEqual(api.calls[:4],['create_job','create_suspended','assign','resume'])
        self.assertEqual(api.inheritance,(True,True));self.assertFalse(os.get_inheritable(self.fd))
        self.assertIn('close_thread',api.calls);child.stop()
    def test_assignment_failure_never_resumes_and_terminates_suspended_child(self):
        api=FakeWinAPI('assign')
        with self.assertRaises(OSError):self.child(api)
        self.assertNotIn('resume',api.calls)
        self.assertLess(api.calls.index('terminate_process'),api.calls.index('close_job'))
        self.assertIn('close_process',api.calls);self.assertIn('close_thread',api.calls)
        self.assertFalse(os.get_inheritable(self.fd))
    def test_create_failure_closes_job_without_claiming_process(self):
        api=FakeWinAPI('create')
        with self.assertRaises(OSError):self.child(api)
        self.assertEqual(api.calls,['create_job','create_suspended','close_job'])
    def test_resume_failure_cleans_all_handles(self):
        api=FakeWinAPI('resume')
        with self.assertRaises(OSError):self.child(api)
        for item in ('terminate_process','close_job','close_process','close_thread'):self.assertIn(item,api.calls)
    def test_job_remains_running_when_launcher_exits_but_children_live(self):
        api=FakeWinAPI();child=self.child(api);api.code=0
        self.assertTrue(child.running);self.assertEqual(child.returncode,0);child.stop()
    def test_stop_is_idempotent_and_kill_job_precedes_wait(self):
        api=FakeWinAPI();child=self.child(api);child.stop();before=list(api.calls);child.stop()
        self.assertEqual(api.calls,before)
        self.assertLess(api.calls.index('close_job'),api.calls.index('wait'))
        self.assertIn('close_process',api.calls)
    def test_empty_job_releases_handles(self):
        api=FakeWinAPI();child=self.child(api);api.count=0;api.code=0
        self.assertFalse(child.running);self.assertTrue(child.closed)
        self.assertIn('close_job',api.calls)


class OwnedProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='native 测试 ')
        self.root=Path(self.temp.name);self.process=OwnedProcess()
        self.python=str(Path(sys.executable).resolve());self.env=dict(os.environ)
        self.log=self.root/'nested'/'gateway.log'
    async def asyncTearDown(self):
        await self.process.stop();self.temp.cleanup()
    async def start(self,code,*args):
        return await self.process.start([self.python,'-u','-c',code,*args],self.root,self.env,self.log)
    async def until(self,predicate,timeout=5):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if predicate():return
            await asyncio.sleep(.02)
        self.fail('native process condition timed out')
    async def test_start_stop_restart_and_no_shell_expansion(self):
        literal='a & b; $HOME " 中文'
        pid=await self.start('import sys,time;print(sys.argv[1],flush=True);time.sleep(30)',literal)
        self.assertTrue(self.process.running);self.assertEqual(self.process.pid,pid)
        await self.until(lambda:self.log.exists() and literal in self.log.read_text())
        self.assertEqual(await self.start('raise RuntimeError("must not start")'),pid)
        await self.process.stop();self.assertFalse(self.process.running);self.assertIsNone(self.process.pid)
        await self.process.stop();await self.process.close()
        second=await self.start('import time;print("restarted",flush=True);time.sleep(30)')
        self.assertNotEqual(second,pid)
        await self.until(lambda:'restarted' in self.log.read_text())
        self.assertIn(literal,self.log.read_text())
    async def test_concurrent_start_has_one_owned_pid(self):
        code='import time;time.sleep(30)'
        result=await asyncio.gather(*(self.start(code) for _ in range(8)))
        self.assertEqual(len(set(result)),1)
    async def test_stdout_and_stderr_large_output_never_wait_for_pipe_reader(self):
        await self.start('import sys,time;sys.stdout.write(("x"*1023+"\\n")*1024);sys.stdout.flush();print("stderr-marker",file=sys.stderr,flush=True);time.sleep(30)')
        await self.until(lambda:self.log.exists() and self.log.stat().st_size>1048576)
        self.assertIn('stderr-marker',self.log.read_text())
    async def test_child_environment_and_working_directory_are_explicit(self):
        self.env['OWNED_TEST']='independent'
        await self.start('import os,time;print(os.environ["OWNED_TEST"],os.getcwd(),flush=True);time.sleep(30)')
        await self.until(lambda:self.log.exists() and str(self.root) in self.log.read_text())
        self.assertIn('independent',self.log.read_text())
    async def test_stdin_is_eof(self):
        await self.start('import sys,time;print(repr(sys.stdin.read()),flush=True);time.sleep(30)')
        await self.until(lambda:self.log.exists() and "''" in self.log.read_text())
    async def test_invalid_command_and_environment_spawn_nothing(self):
        cases=[([],self.env),(['python','x'],self.env),([self.python,'bad\0argument'],self.env),([self.python],{'BAD=KEY':'v'}),([self.python],{'A':'v\0x'})]
        for argv,env in cases:
            with self.assertRaises(ValueError):await self.process.start(argv,self.root,env,self.log)
            self.assertFalse(self.process.running);self.assertIsNone(self.process.pid)
    async def test_failed_executable_does_not_leave_running_state(self):
        file=self.root/'not-executable';file.write_text('not a program')
        with self.assertRaises(OSError):await self.process.start([str(file)],self.root,self.env,self.log)
        self.assertFalse(self.process.running)
    @unittest.skipIf(os.name=='nt','POSIX mode bits and symlink fixture')
    async def test_log_private_and_symlink_rejected(self):
        await self.start('import time;time.sleep(30)')
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode),0o600)
        await self.process.stop();link=self.root/'linked';link.symlink_to(self.log)
        with self.assertRaises(ValueError):await self.process.start([self.python,'-c','pass'],self.root,self.env,link)
    async def test_cancelled_start_waiting_for_lock_spawns_nothing(self):
        async with self.process._lock:
            task=asyncio.create_task(self.start('import time;time.sleep(30)'))
            await asyncio.sleep(0);task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertFalse(self.process.running);self.assertIsNone(self.process.pid)
    async def test_repeated_shutdown_cancellation_finishes_cleanup_before_return(self):
        finished=threading.Event();started=threading.Event()
        class Child:
            closed=False;returncode=None;pid=888
            def stop(inner):
                started.set();time.sleep(.08);inner.closed=True;inner.returncode=0;finished.set()
        self.process._child=Child()
        task=asyncio.create_task(self.process.stop())
        await self.until(started.is_set)
        task.cancel();await asyncio.sleep(.005);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(finished.is_set());self.assertIsNone(self.process.pid)
    async def test_filter_precedes_disk_write_even_when_secret_and_utf8_cross_chunks(self):
        secret='private-token-0123456789';url='http://127.0.0.1:6099/webui?token='+secret
        self.process=OwnedProcess(output_filter=lambda line:line.replace(url,'[url removed]').replace(secret,'[secret removed]'))
        code='import os,time;data=("前奏"*800+" "+'+repr(url)+'+" "+'+repr(secret)+'+"\\n").encode();[os.write(1,data[i:i+3]) for i in range(0,len(data),3)];time.sleep(30)'
        await self.start(code)
        await self.until(lambda:self.log.exists() and '[secret removed]' in self.log.read_text())
        data=self.log.read_text();self.assertNotIn(secret,data);self.assertNotIn(url,data)
        self.assertIn('前奏',data);self.assertNotIn('�',data)
    async def test_newline_free_huge_output_is_bounded_and_never_partially_leaks(self):
        secret='TOP-SECRET-NEVER-WRITE'
        self.process=OwnedProcess(output_filter=lambda line:line.replace(secret,'[removed]'))
        await self.start('import sys,time;sys.stdout.write('+repr(secret)+'*100000);sys.stdout.write("\\nfinished\\n");sys.stdout.flush();time.sleep(30)')
        await self.until(lambda:self.log.exists() and 'finished' in self.log.read_text())
        data=self.log.read_text();self.assertNotIn(secret,data);self.assertIn('omitted overlong',data)
        self.assertLess(len(data),1024);self.assertLess(len(self.process._pump._buffer),16385)
    async def test_filter_failure_omits_raw_text_and_reader_does_not_stall(self):
        def broken(line):raise RuntimeError('filter error')
        self.process=OwnedProcess(output_filter=broken)
        await self.start('import time;print("private-credential",flush=True);time.sleep(30)')
        await self.until(lambda:self.log.exists() and 'redaction failed' in self.log.read_text())
        self.assertNotIn('private-credential',self.log.read_text())
        self.assertEqual(self.process._pump.error,'log_filter_failed')
        pump=self.process._pump;await self.process.stop();self.assertFalse(pump.thread.is_alive())
    async def test_log_byte_limit_keeps_draining_without_growing_file(self):
        with patch.object(native._LogPump,'MAX_LOG_BYTES',4096):
            await self.start('import sys,time;sys.stdout.write(("x"*99+"\\n")*2000);sys.stdout.flush();time.sleep(30)')
            await asyncio.sleep(.15)
            self.assertTrue(self.process.running);self.assertLessEqual(self.log.stat().st_size,4096)
            pump=self.process._pump;await self.process.stop();self.assertFalse(pump.thread.is_alive())
    async def test_stop_drains_final_complete_record_and_closes_reader(self):
        self.process=OwnedProcess(output_filter=lambda line:line.replace('a-secret','[removed]'))
        await self.start('import sys,time;sys.stdout.write("last-a-secret");sys.stdout.flush();time.sleep(30)')
        await asyncio.sleep(.05);pump=self.process._pump
        await self.process.stop();self.assertFalse(pump.thread.is_alive())
        self.assertIn('last-[removed]',self.log.read_text());self.assertNotIn('a-secret',self.log.read_text())
    async def test_fake_windows_start_uses_same_public_lifecycle(self):
        api=FakeWinAPI()
        with patch.object(native,'_WINDOWS',True),patch.object(native,'_WinAPI',return_value=api):
            pid=await self.start('pass');self.assertEqual(pid,1234)
            self.assertTrue(self.process.running);await self.process.stop()
            self.assertFalse(self.process.running)
        self.assertIn('terminate_job',api.calls)


@unittest.skipIf(os.name=='nt','POSIX process-group containment')
class PosixTreeTests(OwnedProcessTests):
    async def tree(self,root_exits=False):
        marker=self.root/'child.pid';heartbeat=self.root/'heartbeat'
        child='import os,signal,time;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);Path("child.pid").write_text(str(os.getpid()));\nwhile True: Path("heartbeat").write_text(str(time.time()));time.sleep(.02)'
        parent='import subprocess,sys,time;subprocess.Popen([sys.executable,"-u","-c",sys.argv[1]]);'+('' if root_exits else 'time.sleep(30)')
        pid=await self.start(parent,child)
        await self.until(lambda:marker.exists() and heartbeat.exists())
        return pid,int(marker.read_text()),heartbeat
    async def assert_heartbeat_stopped(self,heartbeat):
        await asyncio.sleep(.08);before=heartbeat.read_bytes();await asyncio.sleep(.10)
        self.assertEqual(heartbeat.read_bytes(),before)
    async def test_owned_tree_killed_unrelated_process_left_running(self):
        unrelated=subprocess.Popen([self.python,'-c','import time;time.sleep(30)'],start_new_session=True)
        try:
            pid,child,beat=await self.tree()
            self.assertEqual(os.getpgid(pid),pid);self.assertEqual(os.getpgid(child),pid)
            await self.process.stop();await self.assert_heartbeat_stopped(beat)
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate();unrelated.wait(timeout=5)
    async def test_root_exit_cleans_grandchild_before_reaping_ownership_pid(self):
        pid,child,beat=await self.tree(root_exits=True)
        await self.until(lambda:not self.process.running)
        self.assertEqual(self.process.returncode,0)
        await self.assert_heartbeat_stopped(beat)
    async def test_killed_root_still_cleans_owned_children(self):
        pid,child,beat=await self.tree()
        os.kill(pid,signal.SIGKILL)
        await self.until(lambda:not self.process.running)
        await self.assert_heartbeat_stopped(beat)
    async def test_foreign_reap_never_signals_recycled_group_id(self):
        await self.start('pass')
        child=self.process._child
        child.process.wait(timeout=5)  # simulate an embedding host SIGCHLD handler
        with patch.object(native.os,'killpg') as kill:
            self.assertFalse(self.process.running);await self.process.stop();kill.assert_not_called()


if __name__=='__main__':unittest.main()
