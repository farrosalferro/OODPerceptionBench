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

    def setup(self, path_to_conf_file, save_name=None):
        """Initialise the agent.

        **The second parameter is not optional in practice, despite its default.** The stock
        Leaderboard 2.0 base class declares ``setup(self, path_to_conf_file)``, and this agent
        matched it exactly -- which is precisely why it could not run. The pinned Bench2Drive
        evaluator diverges from stock and calls::

            # self.agent_instance.setup(args.agent_config)      <- stock, commented out upstream
            self.agent_instance.setup(args.agent_config, save_name)

        so a strictly-stock agent dies with ``TypeError: setup() takes 2 positional arguments
        but 3 were given`` *before the simulation starts*, and the route settles as
        ``Failed - Agent couldn't be set up``. Every real agent in this ecosystem defends the
        same way -- carla_garage's own `team_code` agents declare
        ``setup(self, path_to_conf_file, route_index=None, traffic_manager=None)``.

        Accepting it with a default keeps this agent valid under BOTH callers. Found by the
        first hardware validation run, 2026-08-11; see runner/README.md "Bringing your own
        agent", which documents the requirement for anyone porting a stock agent.
        """
        self.track = Track.SENSORS
        self._target_speed = float(os.environ.get("OODBENCH_TARGET_SPEED",
                                                  self.TARGET_SPEED))
        # Seed reaches the agent the same way it reaches every other agent in this benchmark:
        # the SEED environment variable, set by the runner from (route, repetition) alone.
        self._seed = int(os.environ.get("SEED", "42"))
        self._save_name = save_name

        # Prove the SAVE_PATH plumbing too. The runner exports SAVE_PATH and mirrors it into
        # the result tree; nothing else in the release demonstrates that an agent can actually
        # write there, and "the agent log directory exists but is empty" is indistinguishable
        # from "the export was wrong". One tiny file makes the difference observable.
        save_path = os.environ.get("SAVE_PATH")
        if save_path:
            try:
                os.makedirs(save_path, exist_ok=True)
                with open(os.path.join(save_path, "reference_agent.txt"), "w",
                          encoding="utf-8") as fh:
                    fh.write(f"seed={self._seed}\ntarget_speed={self._target_speed}\n"
                             f"save_name={save_name}\n")
            except OSError:
                pass  # a plumbing probe must never be the reason a route fails

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
