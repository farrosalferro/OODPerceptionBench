# `config/` — machine configuration

This is the one place for anything machine-specific. A path, hostname, GPU index, port or
environment name hardcoded anywhere else in the repository is a bug;
`tools/check_no_cluster_paths.py` fails CI on the ones it knows.

- `example.yaml` — the template the Quick start copies. It holds what every machine must supply
  (CARLA, the patched checkout, your agent and the Python it runs in, an output folder) as
  `<...>` placeholders with no working defaults, plus the protocol settings: base seed 42, three
  repetitions. With its placeholders filled in it is a valid runner config;
  `runner/tests/test_shipped_configs.py` keeps it that way.
- Your own `<machine>.yaml`, which you pass as `--config`.

Do not commit a config that points at a real machine. Add your own config to `.gitignore` if it
names internal hosts.

The schema is the runner's. Every other field (ports, retries, timeouts, resume, the SLURM
backend) is documented, with its default, in
[`runner/configs/example.yaml`](../runner/configs/example.yaml); you can copy any section of that
file into yours. Reading YAML needs PyYAML in the Python that runs the runner
(`pip install pyyaml`). A `.json` file with the same structure needs only the standard library.
