# OOD-PerceptionBench

**A closed-loop CARLA benchmark that tests how end-to-end driving models react to objects they
have never seen, and separates *visual* shift (a new look) from *geometric* shift (a new shape
or size).**

> **⚠️ Pre-release.** The paper is not posted yet, and this repository has no tag and no DOI.
> Please do not cite it or report numbers from it yet. A `v0.9.0` tag and a Zenodo DOI will
> follow when the paper goes to arXiv.

## Highlights

- **Same scenario, different object.** Every route exists at three levels with identical
  waypoints, weather, traffic and triggers. Only the object the ego vehicle must react to
  changes, so any score difference comes from the object.
- **475 routes** in three categories (pedestrian, static obstacle, vehicle), with results for
  **17 published end-to-end models** over three seeds.
- **Texture-robust, geometry-fragile.** A new shape costs about **2.5×** as much as a new look
  (mean driving-score drop 12.8 vs 5.0). The geometric drop is significant for all 17 models,
  while 9 of 17 are robust to the visual one.

| Level | What changes | Example |
|---|---|---|
| `base` | nothing: a native CARLA object | a standard adult pedestrian |
| `visual_shift` | new **appearance**, familiar **shape** | an astronaut-suited pedestrian |
| `geometric_shift` | new **shape or size** | a delivery robot |

## Contents

- [Installation](#installation)
- [Evaluate your agent](#evaluate-your-agent)
- [Reproduce the paper's numbers](#reproduce-the-papers-numbers)
- [What v0.9 includes](#what-v09-includes)
- [Protocol](#protocol)
- [Documentation](#documentation)
- [License and citation](#license-and-citation)

## Installation

This repository is a small overlay on
[carla_garage](https://github.com/autonomousvision/carla_garage), which includes Bench2Drive.
`setup.sh` fetches the pinned upstream commit and applies our patches.

```bash
git clone https://github.com/farrosalferro/OODPerceptionBench.git
cd OODPerceptionBench
./setup.sh --upstream-dir ./third_party/carla_garage
```

Then:

1. Install **CARLA 0.9.15** and **AdditionalMaps_0.9.15**. 63% of the routes use Town11/12/13,
   which are only in the additional maps.
2. Install the content pack with the new objects: [`assets/INSTALL.md`](assets/INSTALL.md).
3. Create a Python 3.10 environment for your agent:
   `pip install -r env/requirements-pdmlite.txt`, plus your agent's own packages.

> **Check your install once.** If the content pack is missing, CARLA raises no error: the object
> just does not appear, and the route still gets a normal-looking score. Run the acceptance test
> in [`tests/`](tests/) before you trust any number.

Full walkthrough and notes: [`docs/DETAILS.md`](docs/DETAILS.md).

## Evaluate your agent

Agents use the standard CARLA Leaderboard 2.0 interface, the same as Bench2Drive and
carla_garage: an `AutonomousAgent` subclass with `setup()`, `sensors()` and `run_step()`, plus a
module-level `get_entry_point()`.

```bash
cp config/example.yaml config/my_machine.yaml   # fill in every <placeholder>
python runner/run_benchmark.py --config config/my_machine.yaml --dry-run
python runner/run_benchmark.py --config config/my_machine.yaml \
                               --agent  /path/to/your_agent.py \
                               --routes routes/ --out results/
```

Tested on a single GPU and on SLURM at small scale. Several GPUs on one machine are not yet
validated. Options and status: [`runner/README.md`](runner/README.md).

## Reproduce the paper's numbers

No GPU or CARLA needed. [`records/`](records/) holds the per-route results of all 17 models and
the PDM-Lite expert, for seeds 42, 43 and 44:

```python
from records.load import load_records
df = load_records()
```

Column definitions and checks: [`records/README.md`](records/README.md).

## What v0.9 includes

Thirteen of the 18 new objects cannot be redistributed for licensing reasons
([`NOTICE`](NOTICE)). So a fresh install can run 219 of the 475 routes:

| Category | Runnable routes | Total |
|---|---:|---:|
| Pedestrian | 108 | 162 |
| Static | 30 | 70 |
| Vehicle | 81 (base level only) | 243 |
| **Total** | **219** | **475** |

v0.9 matches arXiv v1 of the paper. v1.0 will match arXiv v2 and replace most of the missing
objects.
**Never mix v0.9 and v1.0 scores in one table.**

| | v0.9 (this version) | v1.0 (later) |
|---|---|---|
| Route definitions | 475 | 475 (only replaced objects change) |
| Baseline records | 17 models, seeds 42/43/44 | re-run for replaced objects |
| Content pack | 5 of 18 new objects | replacements for 12 of the other 13 (not the boar) |
| Acceptance goldens | measured PDM-Lite bundle for the 9-route smoke split | regenerate for v1.0 |

## Protocol

- Use seeds **42, 43 and 44** and report the average per route.
- Run the full route set. There is no reportable smaller split; the `smoke` split in
  [`tests/`](tests/) only checks an install.
- State which routes you ran. Scores over different route subsets are not comparable.
- Five routes are left out on purpose: [`routes/EXCLUSIONS.md`](routes/EXCLUSIONS.md).

The statistical tests are in [`docs/DETAILS.md`](docs/DETAILS.md#protocol--do-not-vary-these-if-you-want-comparable-numbers).

## Documentation

| Topic | Where |
|---|---|
| Version stamp, pinned upstream, CI, full install notes, licensing | [`docs/DETAILS.md`](docs/DETAILS.md) |
| Runner and configuration | [`runner/README.md`](runner/README.md) |
| Content pack | [`assets/INSTALL.md`](assets/INSTALL.md) |
| Routes | [`routes/README.md`](routes/README.md) |
| Baseline records | [`records/README.md`](records/README.md) |
| Acceptance tests | [`tests/README.md`](tests/README.md) |
| Building replacement objects | [`docs/README.md`](docs/README.md) |

## License and citation

Our code is MIT ([`LICENSE`](LICENSE)). Four shipped objects are CC BY 4.0 and one,
`walker.pedestrian.firefighter`, is CC BY-NC 4.0 (non-commercial). See [`NOTICE`](NOTICE).

Please cite the paper and this software ([`CITATION.cff`](CITATION.cff)), and also
**Bench2Drive**, **CARLA** and, if you use PDM-Lite or TransFuser++, **carla_garage**. Full
entries are in [`NOTICE`](NOTICE).

Issues and pull requests are welcome.
