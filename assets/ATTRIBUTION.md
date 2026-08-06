# Attribution and licences — OOD-PerceptionBench asset pack v0.9

This pack is **mixed-licence**. Read the table before redistributing or building on it.
Attribution is required for every asset here; one of them is additionally restricted to
**non-commercial** use.

**Version stamp:** asset pack v0.9 ↔ arXiv v1 of the OOD-PerceptionBench paper.

---

## 1. The six 3D assets

| Blueprint ID | Author | Licence | Source | Tarball |
|---|---|---|---|---|
| `walker.pedestrian.astronaut` | **Antropik** | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Sketchfab](https://sketchfab.com/3d-models/astronaut-482bf87662fd4b378bcb3a2931d59ca3) | `walkers-ccby` |
| `walker.pedestrian.deliveryrobot` | **Bento** (`@gostbento`) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Sketchfab](https://sketchfab.com/3d-models/delivery-robot-4dbac67355174751801fb1f6e8dc6230) | `walkers-ccby` |
| `walker.pedestrian.boar` | **AnimalMesh 3D** | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Sketchfab](https://sketchfab.com/3d-models/animated-realistic-boar-3d-animal-model-f672a7fd93e84997b80a54ba30956111) | `walkers-ccby` |
| `static.prop.concreteroadbarrier` | **widthRider** | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Sketchfab](https://sketchfab.com/3d-models/concrete-road-barrier-photoscanned-a09622c043724a1b92e7920b22edb6bf) | `props` |
| `static.prop.roadclosedbarricade` | **exiS7-Gs** | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | [Sketchfab](https://sketchfab.com/3d-models/road-closed-sign-0b8e907e8508406e8560a7adf495d1de) | `props` |
| `walker.pedestrian.firefighter` | **KIFIR** | ⚠ **[CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)** | [Sketchfab](https://sketchfab.com/3d-models/firefighter-tip-b-c62321ea381245f59145efff91c439f4) | `walkers-ccbync` |

### ⚠ The firefighter is NonCommercial

`walker.pedestrian.firefighter`, by **KIFIR**, is licensed **CC BY-NC 4.0**. You may not use
it — or any derivative, including rendered frames and any dataset built from them — for
commercial purposes. It is isolated in its own tarball
(`ood-perceptionbench-walkers-ccbync-v0.9.tar.gz`) precisely so that it can be left out.

If your use is commercial, **do not install that tarball**, and run the verifier with
`--without-nc`. The rest of the pack is CC BY 4.0 and unaffected. The cost of leaving it
out is 18 pedestrian routes (`*_firefighter.xml`), which then cannot be run.

There is no IP problem with this asset — it is KIFIR's own original work — and it is
deliberately **not** being replaced. Bench2Drive, the lineage this benchmark sits in, is
itself CC BY-NC-ND, so an NC component is consistent here.

---

## 2. CARLA base content

`ood-perceptionbench-walkers-ccby-v0.9.tar.gz` also contains one modified CARLA base-content
asset:

```
CarlaUE4/Content/Carla/Blueprints/Walkers/WalkerFactory.uasset
CarlaUE4/Content/Carla/Blueprints/Walkers/WalkerFactory.uexp
```

Derived from [CARLA](https://github.com/carla-simulator/carla) 0.9.15. CARLA's own README
states: *"CARLA specific code is distributed under MIT License. CARLA specific assets are
distributed under CC-BY License."* This file is therefore **CC BY**, © the CARLA Simulator
authors, modified by the OOD-PerceptionBench authors to register the walker blueprints
above. It **overwrites** the corresponding file in your CARLA installation — see
`INSTALL.md` and `WALKERFACTORY_DECISION.md`.

---

## 3. How to cite / credit

If you publish work using this pack, reproduce the attribution table in §1 (author name +
licence + link, per CC BY §3(a)), credit CARLA for the base content, and cite the
OOD-PerceptionBench paper. A `CITATION.cff` ships with the code repository.

Minimal inline form:

> 3D assets: astronaut © Antropik, delivery robot © Bento (@gostbento), boar © AnimalMesh 3D,
> concrete road barrier © widthRider, road-closed barricade © exiS7-Gs — all CC BY 4.0;
> firefighter © KIFIR — CC BY-NC 4.0. Simulator content © CARLA Simulator authors, CC BY.

---

## 4. The twelve assets that are *not* here

Twelve of the eighteen OOD props used in the paper are not redistributable and are
**specified dimensionally instead** — the Appendix tables give their measurements, and the
classifier notebooks (`classifier/{static,pedestrian,vehicle}_dimension_checker.ipynb` in
the code repository) let you check any substitute you source yourself against the same
visual/geometric admissibility rule the paper uses.

Reasons, in brief:

- **Nine marketplace assets** (Fab) carry a Standard Licence that forbids standalone
  redistribution *and* an "Allows usage with AI: No" tag. Both would have to be waived.
- **One** (CGTrader) is Royalty-Free with both redistribution and AI-use restrictions.
- **Two** free downloads were tagged CC-BY by uploaders who did not own the underlying IP.
  They are not licensable by anyone and are being replaced.

The full audit, one row per asset, is `ASSETS.tsv` in the code repository.
