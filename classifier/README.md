# `classifier/` — the visual / geometric admissibility rule

These notebooks check a prop's shift class mechanically. Each takes a candidate prop's bounding
box, compares it with the reference-prop cluster for its category, and returns `visual` or
`geometric`. It is the same rule that assigned every prop in the paper.

They matter most for the **thirteen props we cannot ship**. Those are specified by their dimensions
(paper appendix + [`../docs/replacing-props.md`](../docs/replacing-props.md)). Use these
notebooks to confirm that a substitute you sourced lands in the same class. A substitute that
lands in a different class is not a substitute: it changes what the route measures.

| Notebook | Category | Rule |
|---|---|---|
| `static_dimension_checker.ipynb` | static props | relative-size test against the reference prop: **≤ 20 %** on every dimension → visual; **> 20 %** on any dimension → geometric |
| `pedestrian_dimension_checker.ipynb` | walkers | z-score against the child-walker cluster (**Z ≤ 2** visual, **Z > 3** geometric) *and* a **20 %** relative-difference test against the zero-variance adult-walker cluster |
| `vehicle_dimension_checker.ipynb` | vehicles | z-score against the reference-vehicle cluster (**Z ≤ 2** visual, **Z > 3** geometric), taken on the worst dimension |

The walker and vehicle rules have an **ambiguous band**, `2 < Z ≤ 3`, on purpose: a candidate
there is rejected, not assigned. The static rule has no gap (a single 20 % cut), and no shipped
prop is near it (the statics fall at ≤ 17.8 % or ≥ 73.9 %).

**To check your own candidate**, replace its dimensions in the first cell and re-run. The
reference-cluster statistics are embedded, so nothing is fetched. The notebooks need no CARLA,
GPU or content pack; they run on a laptop with numbers you read off a mesh.
