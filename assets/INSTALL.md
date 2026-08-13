# Installing the OOD-PerceptionBench asset pack v0.9

**Target:** an official **CARLA 0.9.15** Linux build (the packaged release, the one with
`CarlaUE4.sh` and `ImportAssets.sh` at its root). Not the source tree, not any other
version.

> **This pack overwrites one file in CARLA's base content**
> (`CarlaUE4/Content/Carla/Blueprints/Walkers/WalkerFactory.{uasset,uexp}`). That is
> unavoidable — see `WALKERFACTORY_DECISION.md`. It hard-locks the pack to 0.9.15 and it
> is why you should install into a **copy** of CARLA reserved for this benchmark rather
> than a shared one.

---

## 0. Prerequisite — CARLA's additional maps

**Before the asset pack, your CARLA build needs `AdditionalMaps_0.9.15`.** The packaged CARLA
0.9.15 release ships Town01–Town07 and Town10 only. Town11, Town12 and Town13 are a separate
download, and **63% of this benchmark's routes are set in them**:

| category | routes needing the additional maps |
|---|---|
| static | 49 of 70 (70%) |
| pedestrian | 90 of 162 (55%) |
| vehicle | 162 of 243 (66%) |
| **total** | **301 of 475 (63%)** |

```bash
cd /path/to/CARLA_0.9.15
mkdir -p Import && cd Import
wget https://carla-releases.s3.us-east-005.backblazeb2.com/Linux/AdditionalMaps_0.9.15.tar.gz
cd .. && ./ImportAssets.sh
```

Verify before going further — this must list `Town11`, `Town12` and `Town13`:

```bash
ls /path/to/CARLA_0.9.15/CarlaUE4/Content/Carla/Maps/ | grep -E '^Town1[123]$'
```

> **The nine-route golden bundle cannot catch this.** Its routes are set in Town02, Town03 and
> Town04, all of which are in the base build. A CARLA install without the additional maps passes
> every golden and then fails on roughly two thirds of the benchmark. Run the check above rather
> than inferring map coverage from a green golden run.

---

## 1. What you get

| Tarball | Contents | Licence | Download |
|---|---|---|---|
| `ood-perceptionbench-props-v0.9.tar.gz` | `static.prop.concreteroadbarrier`, `static.prop.roadclosedbarricade` | CC BY 4.0 | 15.8 MB |
| `ood-perceptionbench-walkers-ccby-v0.9.tar.gz` | `walker.pedestrian.astronaut`, `.deliveryrobot`, `.boar` **+ `WalkerFactory`** | CC BY 4.0 | 38.4 MB |
| `ood-perceptionbench-walkers-ccbync-v0.9.tar.gz` | `walker.pedestrian.firefighter` | **CC BY-NC 4.0** | 112.3 MB |

**166.6 MB to download, 185.5 MB on disk, 195 files.**

The props tarball touches nothing outside its own content directories and is safe to
install anywhere. The two walker tarballs are useless without `WalkerFactory`, which is in
the `walkers-ccby` tarball: **if you want the firefighter you must also install
`walkers-ccby`**, even if you do not want the other three walkers.

If your use is commercial, skip `walkers-ccbync` (see [`ATTRIBUTION.md`](ATTRIBUTION.md)).

### Where to get them

The tarballs are **not in the git repository** — they are hosted separately because of their
size. Download them from:

```
https://huggingface.co/datasets/farrosalferro24/OODPerceptionBench
```

`SHA256SUMS` in this directory is the authority on what you should have received; verify against
the copy in the repository, not against one downloaded alongside the tarballs.

---

## 2. Install

```bash
cd /path/to/CARLA_0.9.15          # the directory containing CarlaUE4.sh

# 1. verify what you downloaded, against SHA256SUMS from the git repository
sha256sum -c /path/to/OODPerceptionBench/assets/SHA256SUMS

# 2. drop the tarballs in Import/  (omit the ccbync one for commercial use)
mkdir -p Import
cp ood-perceptionbench-*-v0.9.tar.gz Import/

# 3. unpack them into the build
./ImportAssets.sh
```

`ImportAssets.sh` is CARLA's own script. It runs
`find Import/ -type f -name "*.tar.gz"` and extracts each one from the CARLA root, so the
tarballs are rooted at `CarlaUE4/` and land in the right place with no `--strip-components`
or path juggling. You can equivalently do it by hand:

```bash
tar --keep-newer-files -xvf Import/ood-perceptionbench-props-v0.9.tar.gz
```

### The `--keep-newer-files` trap

`ImportAssets.sh` passes `--keep-newer-files`, which makes tar **silently skip** any member
whose modification time is *older* than the file already on disk. Members in this pack are
dated 2026; stock CARLA 0.9.15 content is dated 2023-11-10, so extraction proceeds
normally. But if you have previously copied files into `Content/Carla/Blueprints/Walkers/`
by hand and given them a current timestamp, `WalkerFactory` will not be replaced, no error
will be printed, and none of the four walkers will register. **Step 3 below catches this.**
Tested: installing over a stand-in "stock" `WalkerFactory` dated 2023-11-10 replaces it
correctly (post-install sha256 matches the pack's).

---

## 3. Verify — do not skip this

A missing CARLA asset does not raise. If a blueprint ID is absent, the benchmark harness
substitutes a *different* actor (`vehicle.tesla.model3` for a pedestrian) with a single
stdout warning, and the route completes with a plausible Driving Score. Nothing in the
result JSON records that the wrong thing was measured. A five-minute check now is the
difference between a number and a meaningless number.

```bash
# terminal 1
./CarlaUE4.sh -carla-server -RenderOffScreen -carla-rpc-port=2000

# terminal 2 — needs the `carla` python module from this build's PythonAPI.
# Run it from the REPOSITORY, not from the CARLA root you started the server in.
cd <this-repository>/assets
python3 tools/verify_pack.py --port 2000          # add --without-nc if you skipped the NC tarball
```

Expected tail:

```
VERIFY OK — all six shipped assets registered, spawned and matched reference dimensions;
no phantom ID is spawnable.
```

Exit code 0 means installed correctly. Anything else: stop, fix, re-verify.

The verifier checks that each of the six IDs **spawns**, not merely that it is registered.
That distinction matters: the four walkers are registered by `WalkerFactory`, and a
`WalkerFactory` entry with no content behind it still appears in the blueprint library.

---

## 4. Phantom blueprint IDs — expected, harmless, unusable

After installing, `world.get_blueprint_library()` will advertise **nine walker IDs whose
content is not in this pack**:

```
walker.pedestrian.soldier        walker.pedestrian.wheelchair
walker.pedestrian.ball           walker.pedestrian.caneman
walker.pedestrian.cow            walker.pedestrian.crutcheswoman
walker.pedestrian.deer           walker.pedestrian.labrador
walker.pedestrian.tire
```

They are artefacts of shipping a `WalkerFactory` cooked from a build with more assets than
this pack contains. All nine are **unspawnable**: `try_spawn_actor` returns `None`,
`spawn_actor` raises `RuntimeError: Spawn failed because of invalid actor description`.
They cannot silently give you a wrong prop. Do not use them. `verify_pack.py` asserts that
none of them spawns.

`soldier` and `wheelchair` are real benchmark props whose source models are not
redistributable; the other seven belong to unrelated experiments and appear in no
benchmark route.

---

## 5. What you can run with this pack

**237 of the benchmark's 475 routes.**

| Category | Runnable | Total | What is missing |
|---|---|---|---|
| static | **30** | 70 | 40 routes need 4 non-redistributable props |
| pedestrian | **126** | 162 | 36 routes need `soldier` / `wheelchair` |
| vehicle | **81** | 243 | 162 routes need 6 non-redistributable vehicles |

The 145 `base`-level routes need no pack at all — they use native CARLA blueprints
(`walker.pedestrian.0001/0014/0028`, `static.prop.trafficwarning`,
`vehicle.*.cooper_s_2021/coupe_2020/mkz_2020`). The pack adds the 92 shift-level routes
covered by its six assets: 18 each for astronaut, firefighter, boar and deliveryrobot;
10 each for concreteroadbarrier and roadclosedbarricade.

The remaining 238 routes are **not runnable at v0.9** and running them anyway will produce
substituted-actor results that look normal and are not comparable to anything. Replacement
assets and a re-run are the v1.0 scope.

---

## 6. Uninstall

Delete the six content directories, then restore `WalkerFactory` from a pristine CARLA
0.9.15 (there is no copy of the original in this pack — take one before you install if you
care):

```bash
rm -rf CarlaUE4/Content/{Astronaut,Firefighter,DeliveryRobot,Boar,ConcreteRoadBarrier,RoadClosedBarricade}
cp /pristine/CARLA_0.9.15/CarlaUE4/Content/Carla/Blueprints/Walkers/WalkerFactory.* \
   CarlaUE4/Content/Carla/Blueprints/Walkers/
```

---

## 7. Rebuilding the pack from a CARLA build

For provenance. `build/build_asset_pack.sh` reads a CARLA build read-only and reproduces
the three tarballs and both manifests:

```bash
build/build_asset_pack.sh --carla-root /path/to/carla --out /path/to/assets
```

Exclusions applied during the build (each verified unreferenced) are listed with reasons in
`build/EXCLUSIONS.tsv`.
