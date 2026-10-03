# `config/` — machine configuration

**Purpose:** the single place where anything machine-specific lives. If a path, hostname, GPU
index, port, or environment name is hardcoded anywhere else in this repository, that is a bug —
`tools/check_no_cluster_paths.py` fails CI on the ones we know how to spell.

**What belongs here**

- `example.yaml` — the template the Quick start copies. It holds what every machine must supply
  (CARLA, the patched checkout, your agent and the Python it runs in, an output folder) as
  `<...>` placeholders with no working defaults, plus the protocol settings: base seed 42, three
  repetitions. Once its placeholders are filled in it is a valid runner config, and
  `runner/tests/test_shipped_configs.py` keeps it one.
- Your own `<machine>.yaml`, which you pass as `--config`.

**What does not belong here.** Anything committed that points at a real machine. Add your own
config to `.gitignore` if it names internal hosts.

The schema is the runner's. Every other field (ports, retries, timeouts, resume, the SLURM
backend) is documented, with its default, in
[`runner/configs/example.yaml`](../runner/configs/example.yaml); any section of that file can be
copied into yours. Reading YAML needs PyYAML in the Python that runs the runner
(`pip install pyyaml`). A `.json` file with the same structure needs nothing beyond the standard
library.
