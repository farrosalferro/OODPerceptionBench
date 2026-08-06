# `assets/` — the redistributable content pack

**Bundle version:** v0.9 · **Binds to:** arXiv v1

**Purpose:** the six OOD props we are allowed to redistribute — how to get them, install them,
verify them, and attribute them.

> ### The binaries are not in this repository
>
> The three cooked tarballs are **166.6 MB** packaged (185.5 MB installed, 195 files), which is
> past what a git repository should carry. They are hosted separately:
>
> **Download:** `https://huggingface.co/datasets/farrosalferro24/OODPerceptionBench`
>
> This directory holds everything *around* the binaries: the installer, the checksums, the
> per-asset attributions, the post-install verifier, and the build scripts that reproduce the
> pack from a CARLA build. `.gitignore` excludes `*.tar.gz`, so a downloaded tarball left here
> cannot be committed by accident.

## What is in the pack

| Blueprint ID | Category | Shift level | Author | Licence |
|---|---|---|---|---|
| `walker.pedestrian.astronaut` | walker | visual | Antropik | CC BY 4.0 |
| `walker.pedestrian.firefighter` | walker | visual | KIFIR | ⚠ **CC BY-NC 4.0** |
| `walker.pedestrian.deliveryrobot` | walker | geometric | Bento (`@gostbento`) | CC BY 4.0 |
| `walker.pedestrian.boar` | walker | geometric | AnimalMesh 3D | CC BY 4.0 |
| `static.prop.concreteroadbarrier` | static | geometric | widthRider | CC BY 4.0 |
| `static.prop.roadclosedbarricade` | static | geometric | exiS7-Gs | CC BY 4.0 |

Plus one modified CARLA base-content asset, `WalkerFactory` (CC BY, © the CARLA authors) — the
four walkers cannot register without it. See
[`WALKERFACTORY_DECISION.md`](WALKERFACTORY_DECISION.md).

> **`firefighter` is NonCommercial.** The pack is therefore mixed-licence, and the NC asset is
> isolated in its own tarball so it can be left out. Commercial users must skip
> `ood-perceptionbench-walkers-ccbync-v0.9.tar.gz`, run the verifier with `--without-nc`, and
> exclude the 18 routes matching `routes/pedestrian/**/*_firefighter.xml`. Full terms and the
> required attribution text: [`ATTRIBUTION.md`](ATTRIBUTION.md) and [`../NOTICE`](../NOTICE).

## Route coverage with this pack

| Category | Runnable | Total |
|---|---:|---:|
| static | **30** | 70 |
| pedestrian | **126** | 162 |
| vehicle | **81** | 243 |
| **total** | **237** | **475** |

145 of those 237 are `base`-level route files that need no pack at all. The pack unlocks the
other 92 shift-level routes. The remaining 238 routes are **not runnable at v0.9** — see
[`../NOTICE`](../NOTICE) §3 and [`../docs/replacing-props.md`](../docs/replacing-props.md).

## Files here

| File | What |
|---|---|
| [`INSTALL.md`](INSTALL.md) | install, verify, uninstall, rebuild — **including the `tar --keep-newer-files` trap** |
| [`ATTRIBUTION.md`](ATTRIBUTION.md) | per-asset author, licence, source link; the required attribution strings |
| [`WALKERFACTORY_DECISION.md`](WALKERFACTORY_DECISION.md) | why a base-content asset has to ship, and the nine phantom blueprint IDs it creates |
| [`SHA256SUMS`](SHA256SUMS) | checksums of the three tarballs — verify before installing |
| [`MANIFEST.tsv`](MANIFEST.tsv) | every shipped file: path, sha256, size, owning asset, licence |
| [`tools/verify_pack.py`](tools/verify_pack.py) | post-install verification against a live CARLA server. **Run it.** |
| [`tools/goldens.json`](tools/goldens.json) | reference bounding boxes for the six assets |
| [`build/`](build/) | reproduces the pack from a CARLA build; `EXCLUSIONS.tsv` records what was deliberately not shipped |

## What does not belong here

The other twelve props, in any form — no meshes, no textures, no cooked packages, **no download
links and no purchase instructions**. Ten of them carry an explicit AI-use prohibition, so
pointing a user at those listings so they can run an AI benchmark would walk them into the same
restriction that closed the path for us. See [`../NOTICE`](../NOTICE) §3 for the reasoning and
[`../docs/replacing-props.md`](../docs/replacing-props.md) for how to substitute your own.

## Two things that will bite you

1. **The pack overwrites base CARLA content.** Walker blueprint IDs are registered in a *cooked*
   `WalkerFactory` asset, not in a JSON file, so the pack must ship that factory — which
   hard-locks it to **CARLA 0.9.15 exactly**. A different CARLA version will not work and will
   not say so. Install into a copy of CARLA reserved for this benchmark.

2. **A failed install is silent.** An unregistered blueprint does not raise on spawn; the prop is
   simply absent and the route scores plausibly. `tools/verify_pack.py` and
   [`../tests/`](../tests/) are the only things that distinguish a correct install from a
   confident wrong one.

**Version stamp:** the v0.9 pack contains 6 of 18 props. The v1.0 pack adds licence-clean
replacements for the rest, and records from the two are **not** comparable on the replaced props.
