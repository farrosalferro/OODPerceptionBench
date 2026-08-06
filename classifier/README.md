# `classifier/` — the visual / geometric admissibility rule

**Bundle version:** v0.9 · **Binds to:** arXiv v1

**Purpose:** this is what makes "a prop's shift class can be checked mechanically" a true
statement rather than a claim. Each notebook takes a candidate prop's bounding box, compares it
against the reference-prop cluster for its category, and returns `visual` or `geometric` — the
same rule that assigned every prop in the paper.

It matters most for the **twelve props we cannot ship**. Those are specified *dimensionally*
(paper appendix + [`../docs/replacing-props.md`](../docs/replacing-props.md)), and these
notebooks are how you confirm a substitute you sourced yourself lands in the same class. A
substitute that reclassifies is not a substitute — it changes what the route measures.

| Notebook | Category | Rule |
|---|---|---|
| `static_dimension_checker.ipynb` | static props | relative-size test against the reference prop: **≤ 20 %** on every dimension → visual; **> 20 %** on any dimension → geometric |
| `pedestrian_dimension_checker.ipynb` | walkers | z-score against the child-walker cluster (**Z ≤ 2** visual, **Z > 3** geometric) *and* a **20 %** relative-difference test against the zero-variance adult-walker cluster |
| `vehicle_dimension_checker.ipynb` | vehicles | z-score against the reference-vehicle cluster (**Z ≤ 2** visual, **Z > 3** geometric), taken on the worst dimension |

The walker and vehicle rules have a deliberate `2 < Z ≤ 3` **ambiguous band**: a candidate that
lands there is neither cleanly in-distribution nor cleanly out, and is rejected rather than
assigned. The static rule has no gap — the boundary is a single 20 % cut — and no shipped prop
sits near it (the statics fall at ≤ 17.8 % or ≥ 73.9 %).

**What does not belong here.** Anything that needs CARLA, a GPU, or the content pack. These run
on a laptop against numbers you can read off a mesh.

**Run them against your own candidate**: replace the candidate's dimensions in the first cell
and re-run. The reference-cluster statistics are embedded, so nothing external is fetched.
