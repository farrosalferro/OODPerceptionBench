"""The shipped reference agent must satisfy the evaluator that actually calls it.

Found by the first hardware validation (H2, 2026-08-11), which is the only way it could have
been found: every one of the other 219 tests drives a stand-in evaluator, and the stand-in calls
nothing on an agent at all.

**What happened.** `runner/README.md` promises the *stock* CARLA Leaderboard 2.0
`AutonomousAgent` interface, and the reference agent implemented it exactly --
`setup(self, path_to_conf_file)`. The pinned Bench2Drive evaluator does not use the stock call.
Its own source shows the divergence explicitly::

    # self.agent_instance.setup(args.agent_config)        <- stock, commented out upstream
    self.agent_instance.setup(args.agent_config, save_name)

So the agent died with `TypeError: setup() takes 2 positional arguments but 3 were given`
*before the simulation started*. The route then settled as `Failed - Agent couldn't be set up`,
a legitimate status, and **the runner correctly exited 0 reporting one complete route** -- so the
documented "prove the plumbing before spending GPU-hours" step failed while looking like it had
worked, unless you read the status field.

**Why this is a source-level test rather than an import.** Importing the agent needs `carla` and
`leaderboard`, which the suite deliberately does not have -- these 220 tests run with no CARLA,
no GPU and no third-party packages, and that property is worth more than the extra strictness.
Parsing the file answers the exact question that mattered: *can the evaluator's call bind?*
"""

import ast
import unittest
from pathlib import Path

AGENT = (Path(__file__).resolve().parent.parent
         / "reference_agent" / "constant_velocity_agent.py")


def _method(name: str) -> ast.FunctionDef:
    tree = ast.parse(AGENT.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{AGENT.name} defines no {name}()")


class TestReferenceAgentBindsToThePinnedEvaluator(unittest.TestCase):

    def test_setup_accepts_the_evaluators_two_argument_call(self):
        """RED BEFORE THE FIX:

            AssertionError: 1 != 2 : the pinned evaluator calls
            setup(args.agent_config, save_name); a one-argument setup raises TypeError before
            the simulation starts

        The bar is the *caller*, not the base class. `leaderboard_evaluator.py` calls
        `self.agent_instance.setup(args.agent_config, save_name)`.
        """
        args = _method("setup").args
        positional = [a.arg for a in args.posonlyargs + args.args if a.arg != "self"]
        self.assertGreaterEqual(
            len(positional), 2,
            "the pinned evaluator calls setup(args.agent_config, save_name); a one-argument "
            "setup raises TypeError before the simulation starts")

    def test_setup_still_binds_to_the_stock_one_argument_call(self):
        """Both callers must work. The stock Leaderboard 2.0 base class declares
        `setup(self, path_to_conf_file)`, and an agent that *required* the second argument would
        break anyone running this file against an unpatched leaderboard. Defaults, not arity,
        are what make it compatible in both directions."""
        args = _method("setup").args
        required = len([a for a in args.posonlyargs + args.args if a.arg != "self"]) - len(args.defaults)
        self.assertLessEqual(
            required, 1,
            "every parameter after path_to_conf_file needs a default, or the agent stops "
            "working under a stock (unpatched) leaderboard")

    def test_the_agent_writes_something_to_SAVE_PATH(self):
        """The runner exports `SAVE_PATH` and mirrors it into the result tree, and nothing else
        in the release demonstrates that an agent can write there. Without this, "the log
        directory exists but is empty" is indistinguishable from "the export was wrong" -- which
        is exactly the ambiguity H2's acceptance item 6 ran into.
        """
        src = AGENT.read_text(encoding="utf-8")
        self.assertIn("SAVE_PATH", src,
                      "the reference agent is a plumbing probe; it must exercise SAVE_PATH")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
