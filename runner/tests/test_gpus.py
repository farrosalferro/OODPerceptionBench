"""``--check-gpus`` must never print a CUDA/Vulkan pair it did not derive from the hardware.

The first hardware validation ran it on a one-GPU host whose Vulkan list held the real GPU at
adapter 0 and ``llvmpipe`` at adapter 1. The report then printed a hard-coded example,
``{cuda: 0, vulkan: 0}`` / ``{cuda: 1, vulkan: 1}`` -- pairing the software rasterizer with a
CUDA device that does not exist. Copied into a config, that line puts CARLA on a CPU renderer
and nothing errors.

These tests mock :func:`oodbench.gpus._run` (and ``shutil.which``), so they need no GPU.
"""

import re
import unittest
import unittest.mock

from oodbench import gpus

SMI_ONE = "0, NVIDIA RTX A6000, 00000000:01:00.0, 49140 MiB, GPU-6f2d1f7a-1111-2222-3333-444455556666\n"
SMI_TWO = (
    "0, NVIDIA RTX A6000, 00000000:01:00.0, 49140 MiB, GPU-aaaaaaaa-0000-0000-0000-000000000001\n"
    "1, NVIDIA RTX A6000, 00000000:41:00.0, 49140 MiB, GPU-bbbbbbbb-0000-0000-0000-000000000002\n"
)

#: ``vulkaninfo --summary`` shape (Vulkan SDK 1.3), trimmed to the fields the parser reads.
VK_ONE_PLUS_LLVMPIPE = """\
==========
VULKANINFO
==========

Devices:
========
GPU0:
\tapiVersion         = 1.3.242
\tvendorID           = 0x10de
\tdeviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
\tdeviceName         = NVIDIA RTX A6000
\tdeviceUUID         = 6f2d1f7a-1111-2222-3333-444455556666
GPU1:
\tapiVersion         = 1.3.230
\tvendorID           = 0x10005
\tdeviceType         = PHYSICAL_DEVICE_TYPE_CPU
\tdeviceName         = llvmpipe (LLVM 15.0.7, 256 bits)
\tdeviceUUID         = 6d657361-3233-2e30-2e31-2d3175627500
"""

#: Two identical boards, listed by Vulkan in the OPPOSITE order to CUDA. Pairing by name is
#: impossible here; only the UUID can say which is which.
VK_TWO_SWAPPED = """\
Devices:
========
GPU0:
\tdeviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
\tdeviceName         = NVIDIA RTX A6000
\tdeviceUUID         = bbbbbbbb-0000-0000-0000-000000000002
GPU1:
\tdeviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
\tdeviceName         = NVIDIA RTX A6000
\tdeviceUUID         = aaaaaaaa-0000-0000-0000-000000000001
GPU2:
\tdeviceType         = PHYSICAL_DEVICE_TYPE_CPU
\tdeviceName         = llvmpipe (LLVM 15.0.7, 256 bits)
"""

#: Same two boards with no UUID anywhere (older vulkaninfo): identical names, so no pair can be
#: derived and the report must say so instead of guessing by position.
VK_TWO_NO_UUID = """\
Devices:
========
GPU0:
\tdeviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
\tdeviceName         = NVIDIA RTX A6000
GPU1:
\tdeviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
\tdeviceName         = NVIDIA RTX A6000
"""

#: The full (non-summary) output's "GPU id" lines, which is what older parsers keyed on.
VK_GPU_ID_LINES = """\
Presentable Surfaces:
=====================
GPU id : 0 (NVIDIA GeForce RTX 3090) [VK_KHR_xcb_surface, VK_KHR_xlib_surface]:
GPU id : 1 (llvmpipe (LLVM 12.0.0, 256 bits)) [VK_KHR_xcb_surface, VK_KHR_xlib_surface]:
"""
SMI_3090 = "0, NVIDIA GeForce RTX 3090, 00000000:01:00.0, 24576 MiB, GPU-12345678-0000-0000-0000-000000000000\n"

PAIR_RE = re.compile(r"\{cuda: (\d+), vulkan: (\d+)\}")


def _fake(smi, vk, have_vulkaninfo=True, have_smi=True):
    def which(name):
        if name == "vulkaninfo":
            return "/usr/bin/vulkaninfo" if have_vulkaninfo else None
        if name == "nvidia-smi":
            return "/usr/bin/nvidia-smi" if have_smi else None
        return None

    def run(cmd):
        if cmd[0] == "nvidia-smi":
            return 0, smi
        if cmd[0] == "vulkaninfo":
            return 0, vk
        raise AssertionError(f"unexpected command {cmd}")

    return (unittest.mock.patch("oodbench.gpus.shutil.which", side_effect=which),
            unittest.mock.patch("oodbench.gpus._run", side_effect=run))


class TestCheckGpusReport(unittest.TestCase):

    def _report(self, *a, **kw):
        w, r = _fake(*a, **kw)
        with w, r:
            return gpus.report()

    def _pairs(self, text):
        return [(int(c), int(v)) for c, v in PAIR_RE.findall(text)]

    def test_one_gpu_plus_llvmpipe_gives_exactly_one_real_pair(self):
        text = self._report(SMI_ONE, VK_ONE_PLUS_LLVMPIPE)
        self.assertEqual(self._pairs(text), [(0, 0)])
        # The software adapter is named as excluded, never paired.
        self.assertIn("llvmpipe", text)
        self.assertNotIn("{cuda: 1", text)

    def test_two_gpus_pair_by_uuid_even_when_vulkan_order_is_reversed(self):
        text = self._report(SMI_TWO, VK_TWO_SWAPPED)
        self.assertEqual(sorted(self._pairs(text)), [(0, 1), (1, 0)])

    def test_identical_names_without_uuid_print_no_pairs(self):
        text = self._report(SMI_TWO, VK_TWO_NO_UUID)
        self.assertEqual(self._pairs(text), [])
        self.assertIn("cannot pair automatically", text)

    def test_missing_vulkaninfo_prints_no_example_pairs(self):
        text = self._report(SMI_TWO, "", have_vulkaninfo=False)
        self.assertEqual(self._pairs(text), [])
        self.assertIn("vulkaninfo not found", text)

    def test_missing_nvidia_smi_prints_no_example_pairs(self):
        text = self._report("", VK_ONE_PLUS_LLVMPIPE, have_smi=False)
        self.assertEqual(self._pairs(text), [])

    def test_gpu_id_line_format_pairs_by_unique_name(self):
        text = self._report(SMI_3090, VK_GPU_ID_LINES)
        self.assertEqual(self._pairs(text), [(0, 0)])


class TestParsers(unittest.TestCase):

    def test_software_adapters_are_dropped(self):
        devs = gpus.parse_vulkan(VK_ONE_PLUS_LLVMPIPE)
        self.assertEqual([(d.index, d.software) for d in devs], [(0, False), (1, True)])

    def test_nvidia_smi_csv(self):
        devs = gpus.parse_cuda(SMI_TWO)
        self.assertEqual([d.index for d in devs], [0, 1])
        self.assertEqual(devs[1].uuid, "bbbbbbbb000000000000000000000002")
        self.assertEqual(devs[1].pci, (0, 0x41, 0, 0))


if __name__ == "__main__":
    unittest.main()
