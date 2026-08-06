"""GPU preflight: help the user establish the CUDA index -> Vulkan adapter index mapping.

They are two independent enumerations. `CUDA_VISIBLE_DEVICES` pins the agent; CARLA renders
with Vulkan and is pinned by `-graphicsadapter` (surfaced as `--gpu-rank`), which does **not**
honour `CUDA_VISIBLE_DEVICES`. On a single-GPU host both indices are 0 and the distinction is
invisible -- which is exactly why it survives testing and then bites on a multi-GPU host.

`run_benchmark.py --check-gpus` prints both lists side by side so the mapping can be recorded
once, in the config, as explicit `{cuda: N, vulkan: M}` pairs.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import List, Tuple


def _run(cmd: List[str]) -> Tuple[int, str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"({cmd[0]} unavailable: {exc})"
    return out.returncode, (out.stdout or "") + (out.stderr or "")


def cuda_devices() -> str:
    if shutil.which("nvidia-smi") is None:
        return "nvidia-smi not found on PATH"
    rc, out = _run(["nvidia-smi",
                    "--query-gpu=index,name,pci.bus_id,memory.total,uuid",
                    "--format=csv,noheader"])
    return out.strip() if rc == 0 else f"nvidia-smi failed:\n{out.strip()}"


def vulkan_devices() -> str:
    if shutil.which("vulkaninfo") is None:
        return ("vulkaninfo not found on PATH (package `vulkan-tools`). Without it the Vulkan "
                "adapter order cannot be confirmed, and the CARLA server may render on a "
                "different GPU than the one the agent uses.")
    rc, out = _run(["vulkaninfo", "--summary"])
    if rc != 0:
        rc, out = _run(["vulkaninfo"])
    # vulkaninfo repeats the device list once per surface/format section; keep the first
    # contiguous run of "GPU id" lines, in order, and de-duplicate.
    seen = []
    for ln in out.splitlines():
        stripped = " ".join(ln.split()).rstrip(":")
        if stripped.startswith("GPU id") and stripped not in seen:
            seen.append(stripped)
    if not seen:
        return out.strip()[:4000]

    lines = list(seen)
    if any("llvmpipe" in s or "lavapipe" in s or "swrast" in s for s in seen):
        lines.append("")
        lines.append(
            "NOTE: a SOFTWARE rasterizer (llvmpipe/lavapipe) is present in the Vulkan device "
            "list. It occupies an adapter index just like a real GPU, so the Vulkan and CUDA "
            "orderings can differ even on a single-GPU host. Never assume vulkan == cuda "
            "without reading this list.")
    return "\n".join(lines)


def report() -> str:
    return "\n".join([
        "=== CUDA devices (nvidia-smi) — index used for CUDA_VISIBLE_DEVICES ===",
        cuda_devices(),
        "",
        "=== Vulkan devices (vulkaninfo) — index used for CARLA -graphicsadapter ===",
        vulkan_devices(),
        "",
        "Match the two lists by device name / PCI bus id and write the pairs into the config:",
        "",
        "  gpus:",
        "    - {cuda: 0, vulkan: 0}",
        "    - {cuda: 1, vulkan: 1}",
        "",
        "If they disagree, the agent and the simulator run on different GPUs and NOTHING will",
        "error: one device saturates, throughput collapses, and results are still produced.",
    ])
