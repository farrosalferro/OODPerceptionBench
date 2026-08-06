"""Reference agent: the smallest thing that exercises the whole runner path.

This is a **stock CARLA Leaderboard 2.0 ``AutonomousAgent``** with no machine learning, no
checkpoint and no third-party dependency beyond the leaderboard itself. It requests one RGB
camera plus the speedometer -- enough that the sensor validation the track performs is real --
and drives at a constant target speed with a trivial proportional controller.

**It is a plumbing test, not a baseline.** It will crash, get blocked and score badly, which is
fine: its job is to prove that CARLA starts on the right GPU, the route loads, the agent
interface binds, criteria attach, and a finalized checkpoint JSON lands in the right place. Use
it before spending ~58 GPU-hours on a real model.

The reference agent for *goldens* is PDM-Lite (privileged perception, stable), which is a
separate deliverable.

Wire it up with ``configs/reference_agent.yaml``.
"""

from __future__ import annotations

import os

import carla
from leaderboard.autoagents.autonomous_agent import AutonomousAgent, Track


def get_entry_point():
    """Required by the leaderboard's agent loader."""
    return "ConstantVelocityAgent"


class ConstantVelocityAgent(AutonomousAgent):
    """Drives forward at a fixed target speed. Steers not at all."""

    #: metres per second
    TARGET_SPEED = 4.0

    def setup(self, path_to_conf_file):
        self.track = Track.SENSORS
        self._target_speed = float(os.environ.get("OODBENCH_TARGET_SPEED",
                                                  self.TARGET_SPEED))
        # Seed reaches the agent the same way it reaches every other agent in this benchmark:
        # the SEED environment variable, set by the runner from (route, repetition) alone.
        self._seed = int(os.environ.get("SEED", "42"))

    def sensors(self):
        return [
            {
                "type": "sensor.camera.rgb",
                "id": "rgb_front",
                "x": 0.8, "y": 0.0, "z": 1.6,
                "roll": 0.0, "pitch": 0.0, "yaw": 0.0,
                "width": 800, "height": 600, "fov": 100,
            },
            {"type": "sensor.speedometer", "id": "speed", "reading_frequency": 20},
        ]

    def run_step(self, input_data, timestamp):
        speed = 0.0
        if "speed" in input_data:
            try:
                speed = float(input_data["speed"][1]["speed"])
            except (KeyError, IndexError, TypeError, ValueError):
                speed = 0.0

        control = carla.VehicleControl()
        error = self._target_speed - speed
        control.throttle = max(0.0, min(0.75, 0.5 * error))
        control.brake = 1.0 if error < -1.0 else 0.0
        control.steer = 0.0
        return control

    def destroy(self):
        pass
