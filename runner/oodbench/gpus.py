"""GPU preflight: help the user establish the CUDA index -> Vulkan adapter index mapping.

They are two independent enumerations. `CUDA_VISIBLE_DEVICES` pins the agent; CARLA renders
with Vulkan and is pinned by `-graphicsadapter` (surfaced as `--gpu-rank`), which does **not**
honour `CUDA_VISIBLE_DEVICES`. On a single-GPU host both indices are 0 and the distinction is
invisible -- which is exactly why it survives testing and then bites on a multi-GPU host.

`run_benchmark.py --check-gpus` prints both lists side by side so the mapping can be recorded
once, in the config, as explicit `{cuda: N, vulkan: M}` pairs.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


def _run(cmd: List[str]) -> Tuple[int, str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"({cmd[0]} unavailable: {exc})"
    return out.returncode, (out.stdout or "") + (out.stderr or "")


#: Vulkan adapters that are CPU renderers. They take an adapter index exactly like a real GPU,
#: so they shift the Vulkan ordering, but CARLA must never be pinned to one.
SOFTWARE_ADAPTERS = ("llvmpipe", "lavapipe", "swrast", "softpipe")


@dataclass
class CudaDevice:
    index: int
    name: str
    pci: Optional[Tuple[int, int, int, int]] = None   # (domain, bus, device, function)
    uuid: Optional[str] = None                        # normalised: lowercase hex, no dashes


@dataclass
class VulkanDevice:
    index: int
    name: str
    software: bool = False
    pci: Optional[Tuple[int, int, int, int]] = None
    uuid: Optional[str] = None


def _norm_uuid(raw: str) -> Optional[str]:
    """``GPU-6f2d...`` (nvidia-smi) and ``6f2d...`` (Vulkan deviceUUID) to one comparable form."""
    s = raw.strip().lower()
    if s.startswith("gpu-"):
        s = s[4:]
    s = s.replace("-", "")
    return s if re.fullmatch(r"[0-9a-f]{32}", s) else None


def _parse_bus_id(raw: str) -> Optional[Tuple[int, int, int, int]]:
    """nvidia-smi's ``00000000:01:00.0`` -> (domain, bus, device, function)."""
    m = re.fullmatch(r"\s*([0-9a-fA-F]+):([0-9a-fA-F]+):([0-9a-fA-F]+)\.([0-7])\s*", raw)
    if not m:
        return None
    return tuple(int(x, 16) for x in m.groups())  # type: ignore[return-value]


def parse_cuda(text: str) -> List[CudaDevice]:
    """Parse ``nvidia-smi --query-gpu=index,name,pci.bus_id,memory.total,uuid`` CSV."""
    devs: List[CudaDevice] = []
    for ln in text.splitlines():
        parts = [p.strip() for p in ln.split(",")]
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        devs.append(CudaDevice(
            index=int(parts[0]), name=parts[1],
            pci=_parse_bus_id(parts[2]) if len(parts) > 2 else None,
            uuid=_norm_uuid(parts[4]) if len(parts) > 4 else None))
    return devs


def _is_software(name: str, device_type: str = "") -> bool:
    low = name.lower()
    return any(s in low for s in SOFTWARE_ADAPTERS) or "TYPE_CPU" in device_type.upper()


def parse_vulkan(text: str) -> List[VulkanDevice]:
    """Parse ``vulkaninfo`` (``--summary`` or full) into one entry per adapter index.

    Two shapes exist in the wild: ``GPU<N>:`` blocks carrying ``deviceName`` / ``deviceType`` /
    ``deviceUUID`` (and, in the full output, ``pciBus`` etc.), and the full output's
    ``GPU id : N (name)`` lines. Blocks are preferred because only they carry an identifier
    stronger than the name.
    """
    blocks: Dict[int, Dict[str, str]] = {}
    current: Optional[int] = None
    for ln in text.splitlines():
        m = re.match(r"^\s*GPU(\d+)\s*:?\s*$", ln)
        if m:
            current = int(m.group(1))
            blocks.setdefault(current, {})
            continue
        if current is None:
            continue
        kv = re.match(r"^\s*(deviceName|deviceType|deviceUUID|pciDomain|pciBus|pciDevice|"
                      r"pciFunction)\s*[=:]\s*(.+?)\s*$", ln)
        if kv and kv.group(1) not in blocks[current]:
            blocks[current][kv.group(1)] = kv.group(2)

    devs: List[VulkanDevice] = []
    for idx in sorted(blocks):
        b = blocks[idx]
        if "deviceName" not in b:
            continue
        pci = None
        try:
            if all(k in b for k in ("pciDomain", "pciBus", "pciDevice", "pciFunction")):
                pci = tuple(int(b[k], 0) for k in ("pciDomain", "pciBus", "pciDevice",
                                                   "pciFunction"))
        except ValueError:
            pci = None
        devs.append(VulkanDevice(
            index=idx, name=b["deviceName"],
            software=_is_software(b["deviceName"], b.get("deviceType", "")),
            pci=pci,  # type: ignore[arg-type]
            uuid=_norm_uuid(b["deviceUUID"]) if "deviceUUID" in b else None))
    if devs:
        return devs

    seen: Dict[int, str] = {}
    for ln in text.splitlines():
        m = re.match(r"^\s*GPU id\s*[:=]\s*(\d+)\s*\((.*)\)", ln)
        if m and int(m.group(1)) not in seen:
            name = m.group(2)
            # "(llvmpipe (LLVM 12.0.0, 256 bits)) [VK_KHR...]" -- cut the extension list off.
            name = re.sub(r"\)\s*\[.*$", "", name)
            seen[int(m.group(1))] = name.strip()
    return [VulkanDevice(index=i, name=n, software=_is_software(n)) for i, n in sorted(seen.items())]


def pair_devices(cuda: List[CudaDevice], vulkan: List[VulkanDevice]
                 ) -> Tuple[List[Tuple[int, int, str]], List[int]]:
    """Derive ``(cuda, vulkan, matched_by)`` pairs from the hardware, never from position.

    Per CUDA device, the strongest identifier both sides carry wins: UUID, then PCI address,
    then a device name that occurs exactly once in each list. A device that none of them
    identifies uniquely is returned unpaired -- guessing by index is the exact failure this
    report exists to prevent. Software adapters are never candidates.
    """
    hw = [v for v in vulkan if not v.software]
    pairs: List[Tuple[int, int, str]] = []
    unpaired: List[int] = []
    used: set = set()
    for c in cuda:
        match: Optional[VulkanDevice] = None
        how = ""
        if c.uuid:
            hits = [v for v in hw if v.uuid and v.uuid == c.uuid]
            if len(hits) == 1:
                match, how = hits[0], "UUID"
        if match is None and c.pci:
            hits = [v for v in hw if v.pci and v.pci == c.pci]
            if len(hits) == 1:
                match, how = hits[0], "PCI bus id"
        if match is None:
            same_cuda = [x for x in cuda if x.name == c.name]
            hits = [v for v in hw if v.name == c.name]
            if len(same_cuda) == 1 and len(hits) == 1:
                match, how = hits[0], "unique device name"
        if match is None or match.index in used:
            unpaired.append(c.index)
            continue
        used.add(match.index)
        pairs.append((c.index, match.index, how))
    return pairs, unpaired


def _query_cuda() -> Tuple[str, Optional[List[CudaDevice]]]:
    if shutil.which("nvidia-smi") is None:
        return "nvidia-smi not found on PATH", None
    rc, out = _run(["nvidia-smi",
                    "--query-gpu=index,name,pci.bus_id,memory.total,uuid",
                    "--format=csv,noheader"])
    if rc != 0:
        return f"nvidia-smi failed:\n{out.strip()}", None
    return out.strip(), parse_cuda(out)


def _query_vulkan() -> Tuple[str, Optional[List[VulkanDevice]]]:
    if shutil.which("vulkaninfo") is None:
        return ("vulkaninfo not found on PATH (package `vulkan-tools`). Without it the Vulkan "
                "adapter order cannot be confirmed, and the CARLA server may render on a "
                "different GPU than the one the agent uses."), None
    rc, out = _run(["vulkaninfo", "--summary"])
    devs = parse_vulkan(out) if rc == 0 else []
    if not devs:
        rc, out = _run(["vulkaninfo"])
        devs = parse_vulkan(out)
    if not devs:
        return out.strip()[:4000], None

    lines = []
    for d in devs:
        tag = "   <- SOFTWARE rasterizer, never pin CARLA here" if d.software else ""
        uuid = f"  uuid={d.uuid}" if d.uuid else ""
        lines.append(f"{d.index}, {d.name}{uuid}{tag}")
    if any(d.software for d in devs):
        lines.append("")
        lines.append(
            "NOTE: a SOFTWARE rasterizer (llvmpipe/lavapipe) is present in the Vulkan device "
            "list. It occupies an adapter index just like a real GPU, so the Vulkan and CUDA "
            "orderings can differ even on a single-GPU host. Never assume vulkan == cuda "
            "without reading this list.")
    return "\n".join(lines), devs


def cuda_devices() -> str:
    return _query_cuda()[0]


def vulkan_devices() -> str:
    return _query_vulkan()[0]


def report() -> str:
    cuda_text, cuda = _query_cuda()
    vk_text, vulkan = _query_vulkan()
    out = [
        "=== CUDA devices (nvidia-smi) — index used for CUDA_VISIBLE_DEVICES ===",
        cuda_text,
        "",
        "=== Vulkan devices (vulkaninfo) — index used for CARLA -graphicsadapter ===",
        vk_text,
        "",
    ]
    # Only pairs derived from the two lists above are ever printed. A hard-coded example here
    # once paired a one-GPU host's llvmpipe adapter with a CUDA device that did not exist.
    if cuda is None or vulkan is None:
        missing = "nvidia-smi" if cuda is None else "vulkaninfo"
        out += [f"No pairs can be derived: the {missing} list above is unavailable. Match the "
                f"devices by name / PCI bus id on a host where both tools run, and write them "
                f"into the config's `gpus:` list as {{cuda: N, vulkan: M}}."]
    else:
        pairs, unpaired = pair_devices(cuda, vulkan)
        if pairs:
            out += ["Pairs derived from the lists above (write these into the config):", "",
                    "  gpus:"]
            out += [f"    - {{cuda: {c}, vulkan: {v}}}   # matched by {how}"
                    for c, v, how in pairs]
        for c in unpaired:
            out.append(f"cuda {c}: cannot pair automatically — match by name / PCI bus id "
                       f"(e.g. `vulkaninfo` without --summary lists pciBus) and write the "
                       f"pair by hand.")
        if not cuda:
            out.append("No CUDA devices were listed, so there is nothing to pair.")
    out += [
        "",
        "If they disagree, the agent and the simulator run on different GPUs and NOTHING will",
        "error: one device saturates, throughput collapses, and results are still produced.",
    ]
    return "\n".join(out)
