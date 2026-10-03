"""Per-port run locks: a local run owns its block only once it holds the locks and probed free.

The bug these close: a free probe followed by a wait (the startup wait, then the environment
preflight, tens of seconds) is not ownership. A second run on the same block could probe it free
in that window; once both believed they owned it, each one's ``can_submit`` and ``shutdown``
reaped the other's CARLA as an orphan. Every test here uses a private lock directory -- never
the real one -- and stubs the socket probe and the reaper, so no port or process on the machine
is touched.
"""

import logging
import os
import shutil
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from oodbench import EXIT_OK, config as config_mod, ports
from oodbench.backends.local import LocalBackend, LocalBackendError

from tests.test_integration_local import IntegrationBase, Site


def _log():
    log = logging.getLogger("test-port-locks")
    log.addHandler(logging.NullHandler())
    log.propagate = False
    return log


class LockDirBase(unittest.TestCase):

    def setUp(self):
        self.site = Site()
        self.site.add_route("static/s1/base/route_1_a.xml")
        self.lock_dir = tempfile.mkdtemp(prefix="oodbench-test-locks-")
        env = patch.dict(os.environ, {ports.PORT_LOCK_DIR_ENV: self.lock_dir})
        env.start()
        self.addCleanup(env.stop)
        self.log = _log()
        self.backends = []

    def tearDown(self):
        for b in self.backends:
            b._port_locks.release()
        self.site.cleanup()
        shutil.rmtree(self.lock_dir, ignore_errors=True)

    def backend(self, **ports_over):
        over = {"ports": ports_over} if ports_over else {}
        cfg = config_mod.load(self.site.config(**over))
        b = LocalBackend(cfg, self.log)
        self.backends.append(b)
        return b

    def same_block(self, first):
        """A second backend on exactly ``first``'s port block."""
        return self.backend(rpc_base=first.pairs[0].rpc, tm_base=first.pairs[0].tm,
                            stride=10)


@patch("oodbench.backends.local.reap.reap_ports", return_value=[])
@patch("oodbench.backends.local.ports_mod.probe_pairs", return_value=[])
class TestSecondRunIsRefused(LockDirBase):

    def test_same_block_is_refused_at_once_without_probing_or_reaping(self, probe, reap_ports):
        first = self.backend()
        first.preflight()
        self.assertTrue(first._owns_ports)
        probe.reset_mock()

        second = self.same_block(first)
        started = time.monotonic()
        with patch("oodbench.backends.local.time.sleep") as sleep, \
                self.assertRaises(LocalBackendError) as ctx:
            second.preflight()
        self.assertLess(time.monotonic() - started, 5.0)
        sleep.assert_not_called()
        probe.assert_not_called()
        msg = str(ctx.exception)
        self.assertIn("another oodbench run", msg)
        self.assertIn(str(first.pairs[0].rpc), msg)
        self.assertIn("fuser", msg)
        self.assertIn("ports.rpc_base", msg)
        self.assertFalse(second._owns_ports)
        self.assertFalse(second._port_locks.held, "a refused run must keep no partial lock")

        second.shutdown()
        reap_ports.assert_not_called()
        self.assertTrue(first._port_locks.held, "the refused run released the holder's lock")

    def test_partially_overlapping_block_is_refused(self, _probe, _reap):
        first = self.backend()
        first.preflight()
        # Shares only the RPC block's last port (CARLA's secondary port) and nothing else.
        rpc = first.pairs[0].rpc
        second = self.backend(rpc_base=rpc + 2, tm_base=first.pairs[0].tm + 50, stride=10)
        self.assertEqual(set(first._port_locks.ports) & set(second._port_locks.ports),
                         {rpc + 2})
        with self.assertRaises(LocalBackendError) as ctx:
            second.preflight()
        self.assertIn(str(rpc + 2), str(ctx.exception))
        self.assertFalse(second._owns_ports)

    def test_second_run_succeeds_once_the_first_has_shut_down(self, _probe, reap_ports):
        first = self.backend()
        first.preflight()
        second = self.same_block(first)
        with self.assertRaises(LocalBackendError):
            second.preflight()
        second.shutdown()

        first.shutdown()
        reap_ports.assert_called()  # the owner still reaps its own block on the way out
        self.assertFalse(first._port_locks.held)

        third = self.same_block(first)
        third.preflight()
        self.assertTrue(third._owns_ports)
        self.assertTrue(third._port_locks.held)

    def test_locks_are_released_only_after_shutdown_reaps(self, _probe, reap_ports):
        first = self.backend()
        first.preflight()
        held_while_reaping = []
        reap_ports.side_effect = lambda _p: held_while_reaping.append(
            first._port_locks.held) or []
        first.shutdown()
        self.assertTrue(held_while_reaping and all(held_while_reaping),
                        "the lock was dropped before the block was reaped")
        self.assertFalse(first._port_locks.held)


class TestLockLifetimeOnFailedPreflight(LockDirBase):

    def test_a_block_busy_after_the_startup_wait_leaves_no_lock_after_shutdown(self):
        first = self.backend()
        first.cfg.execution["port_release_timeout_s"] = 2
        busy = [(0, first.pairs[0].rpc)]
        with patch("oodbench.backends.local.ports_mod.probe_pairs", return_value=busy), \
             patch("oodbench.backends.local.time.sleep"), \
             patch("oodbench.backends.local.reap.reap_ports") as reap_ports, \
             self.assertRaises(LocalBackendError):
            first.preflight()
        self.assertFalse(first._owns_ports)
        first.shutdown()
        reap_ports.assert_not_called()
        self.assertFalse(first._port_locks.held)
        free = ports.PortLocks(first._port_locks.ports)
        free.acquire()  # free again
        free.release()


class TestLockDirectoryEdgeCases(LockDirBase):

    @patch("oodbench.backends.local.ports_mod.probe_pairs", return_value=[])
    def test_unusable_lock_dir_warns_and_preflight_proceeds(self, _probe):
        missing = os.path.join(self.lock_dir, "does-not-exist")
        with patch.dict(os.environ, {ports.PORT_LOCK_DIR_ENV: missing}):
            b = self.backend()
            with self.assertLogs(self.log, "WARNING") as logs:
                b.preflight()
        self.assertTrue(b._owns_ports, "the probe still guards; the run must go ahead")
        self.assertFalse(b._port_locks.held)
        self.assertEqual(len(b.preflight_warnings), 1)
        self.assertIn(missing, b.preflight_warnings[0])
        self.assertIn(ports.PORT_LOCK_DIR_ENV, b.preflight_warnings[0])
        self.assertTrue(any("could not take the port locks" in m for m in logs.output))

    def test_locks_are_taken_with_the_probe_disabled(self):
        b = self.backend(probe=False)
        with patch("oodbench.backends.local.ports_mod.probe_pairs") as probe:
            b.preflight()
        probe.assert_not_called()
        self.assertTrue(b._owns_ports)
        self.assertTrue(b._port_locks.held)
        with self.assertRaises(ports.PortLockHeld):
            ports.PortLocks(b._port_locks.ports).acquire()

    @patch("oodbench.backends.local.ports_mod.probe_pairs", return_value=[])
    def test_a_lock_file_another_user_created_can_still_be_locked(self, _probe):
        b = self.backend()
        # Stand-in for another user's file: it exists, and we may not write it.
        for port in b._port_locks.ports:
            path = ports.port_lock_path(port, self.lock_dir)
            with open(path, "w"):
                pass
            os.chmod(path, 0o444)
        b.preflight()
        self.assertTrue(b._port_locks.held)
        for port in b._port_locks.ports:
            mode = stat.S_IMODE(os.stat(ports.port_lock_path(port, self.lock_dir)).st_mode)
            self.assertEqual(mode, 0o444, "an existing lock file must not be modified")


class TestPortLocksUnit(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="oodbench-test-locks-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_created_file_is_readable_by_other_users_despite_the_umask(self):
        old = os.umask(0o077)
        try:
            locks = ports.PortLocks([45000], self.dir)
            locks.acquire()
        finally:
            os.umask(old)
        self.addCleanup(locks.release)
        mode = stat.S_IMODE(os.stat(locks.path(45000)).st_mode)
        self.assertEqual(mode, 0o644)

    def test_lock_descriptor_is_not_inherited_by_children(self):
        locks = ports.PortLocks([45000], self.dir)
        locks.acquire()
        self.addCleanup(locks.release)
        for fd in locks._fds.values():
            self.assertFalse(os.get_inheritable(fd))

    def test_a_held_port_releases_the_ones_already_taken_and_names_only_the_held(self):
        holder = ports.PortLocks([45001], self.dir)
        holder.acquire()
        self.addCleanup(holder.release)
        locks = ports.PortLocks([45000, 45001, 45002], self.dir)
        with self.assertRaises(ports.PortLockHeld) as ctx:
            locks.acquire()
        self.assertEqual(ctx.exception.ports, [45001])
        self.assertFalse(locks.held)
        left = ports.PortLocks([45000, 45002], self.dir)
        left.acquire()  # left free
        left.release()

    def test_a_symlink_is_not_followed(self):
        target = os.path.join(self.dir, "victim")
        with open(target, "w"):
            pass
        os.chmod(target, 0o600)
        os.symlink(target, ports.port_lock_path(45003, self.dir))
        with self.assertRaises(OSError):
            ports.PortLocks([45003], self.dir).acquire()
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)

    def test_a_fifo_at_the_lock_path_is_refused_without_hanging(self):
        # A read-only open of a FIFO waits for a writer, so a FIFO planted in a shared /tmp hung
        # the run in preflight. It must fail like an unusable lock file and drop what it took.
        fifo = ports.port_lock_path(45004, self.dir)
        os.mkfifo(fifo)

        def unblock():  # turns a regression into a failure instead of a hung suite
            try:
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass
        timer = threading.Timer(10, unblock)
        timer.start()
        self.addCleanup(timer.cancel)
        locks = ports.PortLocks([45000, 45004], self.dir)
        with self.assertRaises(OSError) as ctx:
            locks.acquire()
        self.assertIn("not a regular file", str(ctx.exception))
        self.assertFalse(locks.held)
        free = ports.PortLocks([45000], self.dir)
        free.acquire()  # 45000 was released
        free.release()

    def test_release_is_idempotent(self):
        locks = ports.PortLocks([45000], self.dir)
        locks.acquire()
        locks.release()
        locks.release()
        self.assertFalse(locks.held)


class TestLockWarningReachesTheReport(IntegrationBase):

    def test_unusable_lock_dir_is_a_report_warning_and_the_sweep_still_runs(self):
        self.site.add_route("static/s1/base/route_1_a.xml")
        missing = os.path.join(self.site.tmp, "no-such-lock-dir")
        with patch.dict(os.environ, {ports.PORT_LOCK_DIR_ENV: missing}):
            code = self.run_cli(self.site.config())
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(any("could not take the port locks" in w
                            for w in self.report()["warnings"]))


if __name__ == "__main__":
    unittest.main()
