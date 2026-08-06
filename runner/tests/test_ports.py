"""Port allocator invariants.

These run on any machine with no GPU and no CARLA, which is the point: the allocator is proved
sound at worker counts far above any real GPU count, independent of hardware.
"""

import unittest

from oodbench import ports


class TestAllocator(unittest.TestCase):

    def test_deterministic_and_pure_in_worker_index(self):
        a = ports.allocate(8, 20000, 30000, 10)
        b = ports.allocate(8, 20000, 30000, 10)
        self.assertEqual([(p.rpc, p.tm) for p in a], [(p.rpc, p.tm) for p in b])
        self.assertEqual(a[0].rpc, 20000)
        self.assertEqual(a[3].rpc, 20030)
        self.assertEqual(a[3].tm, 30030)

    def test_no_duplicate_ports_far_above_any_gpu_count(self):
        # The acceptance criterion asks for N > physical GPU count. 64 is far beyond any
        # single-node GPU count and needs no hardware to check.
        for workers in (1, 2, 5, 8, 16, 33, 64):
            pairs = ports.allocate(workers, 20000, 40000, 10)
            flat = [p for pair in pairs for p in pair.all_ports]
            self.assertEqual(len(flat), len(set(flat)),
                             f"duplicate ports at workers={workers}")
            self.assertEqual(len(pairs), workers)

    def test_rpc_windows_never_touch(self):
        pairs = ports.allocate(64, 20000, 40000, 4)  # minimum legal stride
        for i in range(len(pairs) - 1):
            self.assertLess(max(pairs[i].rpc_window), min(pairs[i + 1].rpc_window))

    def test_carla_window_is_three_ports(self):
        p = ports.allocate(1, 20000, 30000, 10)[0]
        self.assertEqual(list(p.rpc_window), [20000, 20001, 20002])
        self.assertEqual(p.all_ports, (20000, 20001, 20002, 30000))

    def test_stride_below_carla_window_is_rejected(self):
        for stride in (1, 2, 3):
            with self.assertRaises(ports.PortAllocationError):
                ports.allocate(4, 20000, 30000, stride)

    def test_overlapping_rpc_and_tm_blocks_are_rejected(self):
        # 32 workers * stride 10 from 20000 reaches 20312; a tm_base of 20100 sits inside it.
        with self.assertRaises(ports.PortAllocationError) as ctx:
            ports.allocate(32, 20000, 20100, 10)
        self.assertIn("overlap", str(ctx.exception))

    def test_tm_block_below_rpc_block_also_detected(self):
        with self.assertRaises(ports.PortAllocationError):
            ports.allocate(32, 20100, 20000, 10)

    def test_far_apart_blocks_are_accepted(self):
        pairs = ports.allocate(32, 20000, 40000, 10)
        self.assertEqual(len(pairs), 32)

    def test_block_above_65535_is_rejected(self):
        with self.assertRaises(ports.PortAllocationError):
            ports.allocate(64, 65000, 30000, 10)

    def test_privileged_base_is_rejected(self):
        with self.assertRaises(ports.PortAllocationError):
            ports.allocate(1, 80, 30000, 10)

    def test_zero_workers_is_rejected(self):
        with self.assertRaises(ports.PortAllocationError):
            ports.allocate(0, 20000, 30000, 10)

    def test_probe_reports_a_bound_port_as_busy(self):
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("localhost", 0))
            busy_port = s.getsockname()[1]
            s.listen(1)
            self.assertEqual(ports.probe([busy_port]), [busy_port])
        # After the socket closes the port may linger in TIME_WAIT, so we do not assert the
        # inverse here; test_probe_reports_a_free_port_as_free covers that direction.

    def test_probe_reports_a_free_port_as_free(self):
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("localhost", 0))
            port = s.getsockname()[1]
        # Not bound and never listened on: no TIME_WAIT, so it must probe free.
        self.assertEqual(ports.probe([port]), [])


if __name__ == "__main__":
    unittest.main()
