"""Port-scoped CARLA signalling stays narrow and preserves synchronous cleanup semantics."""

import signal
import unittest
from unittest.mock import call, patch

from oodbench import reap


class TestPortScopedSignals(unittest.TestCase):

    def test_terminate_and_kill_helpers_return_immediately_after_the_requested_signal(self):
        with patch("oodbench.reap.find_carla_on_ports", return_value=[101, 202]), \
             patch("oodbench.reap.os.kill") as os_kill:
            self.assertEqual(reap.terminate_carla_on_ports([20000]), [101, 202])
            self.assertEqual(
                os_kill.call_args_list,
                [call(101, signal.SIGTERM), call(202, signal.SIGTERM)],
            )

        with patch("oodbench.reap.find_carla_on_ports", return_value=[303]), \
             patch("oodbench.reap.os.kill") as os_kill:
            self.assertEqual(reap.kill_carla_on_ports([20000]), [303])
            os_kill.assert_called_once_with(303, signal.SIGKILL)

    def test_synchronous_reaper_still_escalates_when_given_no_term_grace(self):
        with patch("oodbench.reap.terminate_carla_on_ports", return_value=[404]), \
             patch("oodbench.reap.kill_carla_on_ports", return_value=[404]) as kill_ports:
            self.assertEqual(reap.reap_ports([20000], grace_s=0), [404])
            kill_ports.assert_called_once_with([20000])


if __name__ == "__main__":
    unittest.main()
