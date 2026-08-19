# `config/` — machine configuration

**Purpose:** the single place where anything machine-specific lives. If a path, hostname, GPU
index, port, or environment name is hardcoded anywhere else in this repository, that is a bug —
`tools/check_no_cluster_paths.py` fails CI on the ones we know how to spell.

**What belongs here**

- `example.yaml` — a commented template listing every key, with no working defaults.
- Your own `<machine>.yaml`, which you pass as `--config`.

**What does not belong here.** Anything committed that points at a real machine. Add your own
config to `.gitignore` if it names internal hosts.

Expected keys (schema is owned by the runner workstream):

```yaml
carla_root:  /path/to/CARLA_0.9.15      # must be 0.9.15 exactly
gpus:        [0, 1]                     # physical GPU ids the pool may use
workers:     2                          # processes; each pins one GPU
ports:                                  # per-worker port allocation
  rpc_base:      2000
  tm_base:       8000
  stride:        50
agent:
  entrypoint:  /path/to/your_agent.py
  env_command: ""                       # e.g. "conda run -n <your-env>"; empty = current env
output_root:  /path/to/results
timeout_s:    1800                      # per route
retries:      1                         # bounded; see runner/README.md
seed:         42                        # base seed; protocol is 3 seeds (42/43/44) via runner repetitions: 3
```
