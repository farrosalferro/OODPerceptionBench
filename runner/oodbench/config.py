"""Configuration schema, loader and validation.

Design constraints (DESIGN.md §2):

* Exactly one config file drives a run. YAML is primary; TOML and JSON are accepted so the
  runner has *no* third-party dependency if you prefer them.
* No field that names a filesystem location, a host, a scheduler queue or an environment has a
  default. A missing required field is a startup error naming the field.
* Validation is total: unknown keys are errors, not warnings, so a typo can never silently
  fall back to a default.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Safe at module level: jobscript imports this module only under TYPE_CHECKING, so there is no
# import cycle. RESERVED_ENV lives there because that module is what exports the variables.
from .jobscript import RESERVED_ENV
from .reap import PORT_REAP_TERM_GRACE_S

_SENTINEL = object()


class ConfigError(Exception):
    """Raised for any malformed, incomplete or self-inconsistent configuration."""


# --------------------------------------------------------------------------------------
# Schema declaration
# --------------------------------------------------------------------------------------
# Each entry is (key, kind, default). ``default is _REQUIRED`` means the user must supply it.
_REQUIRED = _SENTINEL

SCHEMA: Dict[str, Dict[str, Any]] = {
    "benchmark": {
        # None -> filled from oodbench.BENCHMARK_RELEASE / ARXIV_VERSION after coercion.
        "release": ("optstr", None),
        "arxiv_version": ("optstr", None),
        "seed": ("int", 42),
        "repetitions": ("int", 1),
    },
    "carla": {
        "root": ("path", _REQUIRED),
        "client_timeout_s": ("number", 900),
    },
    "leaderboard": {
        "work_dir": ("path", _REQUIRED),
        "root": ("path", _REQUIRED),
        "scenario_runner_root": ("path", _REQUIRED),
        "evaluator": ("str", "leaderboard/leaderboard_evaluator.py"),
    },
    "agent": {
        "entrypoint": ("path", _REQUIRED),
        "config": ("str", ""),
        "track": ("str", "SENSORS"),
        "pythonpath": ("strlist", []),
        "env": ("strmap", {}),
        # Several published models must run from their own repository root (relative asset and
        # config paths). None means "inherit the runner's working directory".
        "working_dir": ("optpath", None),
    },
    "environment": {
        "activate": ("strlist", []),
        "ld_library_path_prepend": ("strlist", []),
        "python": ("str", "python3"),
    },
    "execution": {
        "backend": ("str", "local"),
        "workers": ("int", 1),
        "route_timeout_s": ("int", 3600),
        "poll_interval_s": ("int", 10),
        "post_kill_cooldown_s": ("int", 10),
        "port_release_timeout_s": ("int", 90),
        "allow_gpu_stacking": ("bool", False),
    },
    "ports": {
        "rpc_base": ("int", 20000),
        "tm_base": ("int", 30000),
        "stride": ("int", 10),
        "probe": ("bool", True),
    },
    "retry": {
        "record_budget": ("int", 3),
        "infra_budget": ("int", 3),
        "tickruntime_budget": ("int", 0),
        # Attempts that ended abnormally (wall-clock kill, fault, quarantine kill) while a
        # crash-shaped record was on disk -- ambiguous by construction, because that is exactly
        # what a dying simulator under a surviving evaluator writes. Its own axis so a kill
        # cannot spend the model's record retries (DESIGN.md 6A.5). >= 2 buys one clean re-run
        # before the record is accepted as the answer; it MUST stay bounded, because the
        # alternative is a route that can never settle.
        "killed_budget": ("int", 2),
        "worker_quarantine_after": ("int", 3),
    },
    "resume": {
        "mode": ("str", "skip_terminal"),
    },
    "output": {
        "root": ("path", _REQUIRED),
        "record_carla": ("bool", False),
    },
    "routes": {
        "root": ("path", _REQUIRED),
        "manifest": ("optpath", None),
        "strict_manifest": ("bool", False),
    },
    "slurm": {
        "partition": ("optstr", None),
        "account": ("optstr", None),
        "qos": ("optstr", None),
        "nodelist": ("optstr", None),
        "exclude": ("optstr", None),
        "time": ("str", "02:00:00"),
        "cpus_per_task": ("int", 4),
        "mem": ("str", "24G"),
        "gres": ("str", "gpu:1"),
        "max_parallel": ("int", 8),
        "submit_interval_s": ("number", 1.0),
        "vulkan_index_scope": ("str", "host"),
        "extra_directives": ("strlist", []),
    },
}

#: Keys added to :data:`SCHEMA` *after* output roots with a stamped config digest existed in
#: the wild, mapped to the value they were introduced with. While such a key holds exactly that
#: value it is left out of :meth:`Config.digest`, so a ledger written before the key existed
#: still matches a configuration that is otherwise identical. See :meth:`Config.digest` for why
#: the pinned value is written here instead of being read back out of ``SCHEMA``.
#:
#: Every entry is a promise that the key's *pinned* value reproduces the behaviour of the build
#: that had no such key. ``retry.killed_budget: 2`` qualifies on the config's own terms -- a
#: configuration is the set of knobs the operator turned, and this one turned none. (The
#: accounting model that the key belongs to did change; that is a change of runner version, and
#: the runner version is stamped into every report separately.) A key whose default alters
#: behaviour in a way an operator would want flagged does NOT belong here.
DIGEST_COMPAT_DEFAULTS: Dict[Tuple[str, str], Any] = {
    ("retry", "killed_budget"): 2,
    ("slurm", "vulkan_index_scope"): "host",
    ("execution", "port_release_timeout_s"): 90,
}

VALID_BACKENDS = ("local", "slurm")
VALID_TRACKS = ("SENSORS", "MAP")
VALID_RESUME_MODES = ("skip_terminal", "skip_any_final", "none")
VALID_VULKAN_INDEX_SCOPES = ("host", "allocation")


# --------------------------------------------------------------------------------------
# GPU entries
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class GpuSpec:
    """One physical GPU, addressed by its *two independent* indices.

    ``cuda``   -> ``CUDA_VISIBLE_DEVICES`` locally, or SLURM's global allocation lookup key.
    ``vulkan`` -> CARLA's ``-graphicsadapter`` (via ``--gpu-rank``) for the simulator.

    They are separate enumerations and are not guaranteed equal. A SLURM allocation may also
    give the Vulkan index job-local scope; see DESIGN.md §4. ``vulkan_assumed`` records that we
    defaulted it, so the runner can warn.
    """

    cuda: int
    vulkan: int
    vulkan_assumed: bool = False


@dataclass
class Config:
    benchmark: Dict[str, Any]
    carla: Dict[str, Any]
    leaderboard: Dict[str, Any]
    agent: Dict[str, Any]
    environment: Dict[str, Any]
    execution: Dict[str, Any]
    ports: Dict[str, Any]
    retry: Dict[str, Any]
    resume: Dict[str, Any]
    output: Dict[str, Any]
    routes: Dict[str, Any]
    slurm: Dict[str, Any]
    gpus: List[GpuSpec]
    source_path: Optional[str] = None
    warnings: List[str] = field(default_factory=list)

    # -- convenience accessors -----------------------------------------------------------
    @property
    def seed(self) -> int:
        return int(self.benchmark["seed"])

    @property
    def workers(self) -> int:
        return int(self.execution["workers"])

    @property
    def evaluator_path(self) -> Path:
        return Path(self.leaderboard["root"]) / self.leaderboard["evaluator"]

    def digest(self) -> str:
        """Stable sha256 of the resolved configuration, for the report.

        Two runs with the same digest were driven by the same effective settings, which is what
        makes a report auditable after the fact.

        **Schema growth must not look like a settings change.** ``build`` materialises every
        schema key into the resolved config, so adding one silently changed the digest of every
        configuration that predates it -- and the digest is what the ledger compares on resume,
        so every pre-existing output root started reporting "this output root was produced by a
        DIFFERENT configuration ... use a fresh output root". That is a false alarm whose
        remedy is throwing away 475 routes of finished work, over a key nobody set.

        A key listed in :data:`DIGEST_COMPAT_DEFAULTS` is therefore omitted from the payload
        *while it holds the pinned value*, and only then. The carve-out is per value, not per
        key: set ``retry.killed_budget: 3`` and it is hashed like anything else, because that
        genuinely changes the run. And because the pinned value is written here rather than read
        from :data:`SCHEMA`, changing a *default* later still moves the digest of every config
        that omits the key -- which is correct, since their behaviour would change too.

        What the digest deliberately does not encode is the runner's own version; that is
        stamped separately into every report. The accounting model changing is not the
        configuration changing.
        """
        payload = {
            k: getattr(self, k)
            for k in (
                "benchmark", "carla", "leaderboard", "agent", "environment", "execution",
                "ports", "retry", "resume", "output", "routes", "slurm",
            )
        }
        payload["gpus"] = [{"cuda": g.cuda, "vulkan": g.vulkan} for g in self.gpus]
        for (section, key), pinned in DIGEST_COMPAT_DEFAULTS.items():
            sect = payload.get(section)
            if isinstance(sect, dict) and sect.get(key) == pinned:
                sect = dict(sect)
                sect.pop(key, None)
                payload[section] = sect
        blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def as_dict(self) -> Dict[str, Any]:
        d = {
            k: getattr(self, k)
            for k in (
                "benchmark", "carla", "leaderboard", "agent", "environment", "execution",
                "ports", "retry", "resume", "output", "routes", "slurm",
            )
        }
        d["gpus"] = [{"cuda": g.cuda, "vulkan": g.vulkan} for g in self.gpus]
        return d


# --------------------------------------------------------------------------------------
# Raw file loading
# --------------------------------------------------------------------------------------
def load_raw(path: str | os.PathLike) -> Dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    suffix = p.suffix.lower()
    text = p.read_text(encoding="utf-8")

    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ConfigError(
                f"{p} is YAML but PyYAML is not installed. Either `pip install pyyaml`, or use "
                f"the equivalent .toml/.json config (both are supported with no dependency)."
            ) from exc
        data = yaml.safe_load(text)
    elif suffix == ".toml":
        try:
            import tomllib  # Python >= 3.11
        except ImportError:  # pragma: no cover - environment dependent
            try:
                import tomli as tomllib  # type: ignore
            except ImportError as exc:
                raise ConfigError(
                    f"{p} is TOML and this Python has neither `tomllib` (3.11+) nor `tomli`."
                ) from exc
        data = tomllib.loads(text)
    elif suffix == ".json":
        data = json.loads(text)
    else:
        raise ConfigError(
            f"unsupported config extension '{suffix}' for {p}; use .yaml, .yml, .toml or .json"
        )

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{p}: top level of a config file must be a mapping")
    return data


# --------------------------------------------------------------------------------------
# Coercion / validation helpers
# --------------------------------------------------------------------------------------
def _coerce(section: str, key: str, kind: str, value: Any) -> Any:
    where = f"{section}.{key}"
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where}: expected an integer, got {value!r}")
        return value
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where}: expected a number, got {value!r}")
        return float(value)
    if kind == "bool":
        if not isinstance(value, bool):
            raise ConfigError(f"{where}: expected true/false, got {value!r}")
        return value
    if kind == "str":
        if not isinstance(value, str):
            raise ConfigError(f"{where}: expected a string, got {value!r}")
        return value
    if kind == "optstr":
        if value is None:
            return None
        if not isinstance(value, str):
            raise ConfigError(f"{where}: expected a string or null, got {value!r}")
        return value
    if kind in ("path", "optpath"):
        if value is None:
            if kind == "optpath":
                return None
            raise ConfigError(f"{where}: expected a path, got null")
        if not isinstance(value, str):
            raise ConfigError(f"{where}: expected a path string, got {value!r}")
        return str(Path(os.path.expanduser(value)).resolve()) if value else value
    if kind == "strlist":
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise ConfigError(f"{where}: expected a list of strings, got {value!r}")
        return list(value)
    if kind == "strmap":
        if not isinstance(value, dict):
            raise ConfigError(f"{where}: expected a mapping, got {value!r}")
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise ConfigError(f"{where}: environment variable names must be strings")
            if isinstance(v, bool) or v is None:
                raise ConfigError(
                    f"{where}.{k}: environment values must be strings or numbers "
                    f"(got {v!r}); quote it to be explicit about what the shell receives"
                )
            out[k] = str(v)
        return out
    raise AssertionError(f"unhandled kind {kind!r}")  # pragma: no cover


def _parse_gpus(raw: Any, *, vulkan_index_scope: str = "host") -> List[GpuSpec]:
    if raw is None:
        raise ConfigError(
            "gpus: required. List every GPU the runner may use, e.g.\n"
            "  gpus:\n    - {cuda: 0, vulkan: 0}\n"
            "`cuda` pins the agent via CUDA_VISIBLE_DEVICES; `vulkan` pins the CARLA server via "
            "-graphicsadapter. They are different enumerations -- see DESIGN.md section 4."
        )
    if not isinstance(raw, list) or not raw:
        raise ConfigError("gpus: expected a non-empty list of GPU entries")

    specs: List[GpuSpec] = []
    seen_cuda: Dict[int, int] = {}      # cuda index -> the gpus[] entry that claimed it
    seen_vulkan: Dict[int, int] = {}    # vulkan adapter -> ditto
    for i, entry in enumerate(raw):
        if isinstance(entry, int) and not isinstance(entry, bool):
            entry = {"cuda": entry}
        if not isinstance(entry, dict):
            raise ConfigError(f"gpus[{i}]: expected a mapping like {{cuda: 0, vulkan: 0}}")
        unknown = set(entry) - {"cuda", "vulkan"}
        if unknown:
            raise ConfigError(f"gpus[{i}]: unknown key(s) {sorted(unknown)}")
        if "cuda" not in entry:
            raise ConfigError(f"gpus[{i}]: missing required key 'cuda'")
        cuda = entry["cuda"]
        if isinstance(cuda, bool) or not isinstance(cuda, int) or cuda < 0:
            raise ConfigError(f"gpus[{i}].cuda: expected a non-negative integer, got {cuda!r}")
        assumed = "vulkan" not in entry
        vulkan = cuda if assumed else entry["vulkan"]
        if isinstance(vulkan, bool) or not isinstance(vulkan, int) or vulkan < 0:
            raise ConfigError(
                f"gpus[{i}].vulkan: expected a non-negative integer, got {vulkan!r}"
            )
        if cuda in seen_cuda:
            raise ConfigError(
                f"gpus[{i}].cuda={cuda} is already claimed by gpus[{seen_cuda[cuda]}]; "
                f"each entry must name a distinct physical GPU"
            )
        # Host-visible Vulkan adapters need the same uniqueness, and for a worse reason. Two
        # entries sharing one host adapter put both CARLA servers on one physical GPU. Under an
        # explicitly qualified one-GPU SLURM allocation, however, adapter 0 is job-local: two
        # separate cgroups may reuse that integer while naming different physical GPUs.
        if vulkan in seen_vulkan and vulkan_index_scope == "host":
            other = seen_vulkan[vulkan]
            note = ""
            if assumed or specs[other].vulkan_assumed:
                note = (" One of the two entries omitted `vulkan`, so it defaulted to its "
                        "`cuda` index; write both indices out explicitly.")
            raise ConfigError(
                f"gpus[{i}].vulkan={vulkan} is already claimed by gpus[{other}] "
                f"(cuda={specs[other].cuda}). `vulkan` is CARLA's -graphicsadapter index: two "
                f"entries sharing it run both simulators on ONE physical GPU while their agents "
                f"run on different ones, which fails silently -- Vulkan does not honour "
                f"CUDA_VISIBLE_DEVICES, so nothing errors and throughput just collapses. Run "
                f"`run_benchmark.py --check-gpus` and match the CUDA and Vulkan lists by device "
                f"name / PCI bus id.{note} If you really do intend several workers per GPU, list "
                f"the GPU once and set execution.allow_gpu_stacking."
            )
        seen_cuda[cuda] = i
        seen_vulkan[vulkan] = i
        specs.append(GpuSpec(cuda=cuda, vulkan=vulkan, vulkan_assumed=assumed))
    return specs


# --------------------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------------------
def build(raw: Dict[str, Any], source_path: Optional[str] = None,
          overrides: Optional[Dict[str, Any]] = None) -> Config:
    """Validate ``raw`` against SCHEMA and return a resolved :class:`Config`.

    ``overrides`` holds already-normalised CLI values keyed ``"<section>.<key>"``. They are
    applied *before* validation so a CLI-supplied path satisfies a required field.
    """
    from . import ARXIV_VERSION, BENCHMARK_RELEASE

    raw = dict(raw)
    warnings: List[str] = []

    version = raw.pop("version", 1)
    if version != 1:
        raise ConfigError(f"version: this runner understands config version 1, got {version!r}")

    gpus_raw = raw.pop("gpus", None)

    unknown_sections = set(raw) - set(SCHEMA)
    if unknown_sections:
        raise ConfigError(
            f"unknown config section(s): {sorted(unknown_sections)}. "
            f"Known sections: {sorted(SCHEMA)} plus 'gpus' and 'version'."
        )

    resolved: Dict[str, Dict[str, Any]] = {}
    for section, keys in SCHEMA.items():
        provided = raw.get(section, {})
        if provided is None:
            provided = {}
        if not isinstance(provided, dict):
            raise ConfigError(f"{section}: expected a mapping, got {provided!r}")
        unknown_keys = set(provided) - set(keys)
        if unknown_keys:
            raise ConfigError(
                f"{section}: unknown key(s) {sorted(unknown_keys)}; known: {sorted(keys)}"
            )
        out: Dict[str, Any] = {}
        for key, (kind, default) in keys.items():
            ov_key = f"{section}.{key}"
            if overrides and ov_key in overrides and overrides[ov_key] is not None:
                value = overrides[ov_key]
            elif key in provided:
                value = provided[key]
            elif default is _REQUIRED:
                raise ConfigError(
                    f"{section}.{key} is required and has no default "
                    f"(deliberately: this runner ships zero site-specific defaults)"
                )
            else:
                value = default
            out[key] = _coerce(section, key, kind, value)
        resolved[section] = out

    # ---- cross-field validation --------------------------------------------------------
    if resolved["benchmark"]["release"] is None:
        resolved["benchmark"]["release"] = BENCHMARK_RELEASE
    if resolved["benchmark"]["arxiv_version"] is None:
        resolved["benchmark"]["arxiv_version"] = ARXIV_VERSION
    if resolved["benchmark"]["repetitions"] < 1:
        raise ConfigError("benchmark.repetitions must be >= 1")

    if resolved["execution"]["backend"] not in VALID_BACKENDS:
        raise ConfigError(
            f"execution.backend must be one of {VALID_BACKENDS}, "
            f"got {resolved['execution']['backend']!r}"
        )
    if resolved["execution"]["workers"] < 1:
        raise ConfigError("execution.workers must be >= 1")
    for key in ("route_timeout_s", "poll_interval_s"):
        if resolved["execution"][key] < 1:
            raise ConfigError(f"execution.{key} must be >= 1")
    if resolved["execution"]["post_kill_cooldown_s"] < 0:
        raise ConfigError("execution.post_kill_cooldown_s must be >= 0")
    if resolved["execution"]["port_release_timeout_s"] < 1:
        raise ConfigError("execution.port_release_timeout_s must be >= 1")
    release_timeout = resolved["execution"]["port_release_timeout_s"]
    release_cooldown = resolved["execution"]["post_kill_cooldown_s"]
    if release_timeout < release_cooldown:
        raise ConfigError(
            "execution.port_release_timeout_s must be >= "
            "execution.post_kill_cooldown_s"
        )
    if resolved["execution"]["backend"] == "local" and resolved["ports"]["probe"]:
        minimum_release_timeout = math.ceil(PORT_REAP_TERM_GRACE_S) + release_cooldown
        if release_timeout < minimum_release_timeout:
            raise ConfigError(
                "execution.port_release_timeout_s must cover the CARLA SIGTERM grace plus "
                "post-kill cooldown when local port probing is enabled: expected >= "
                f"{minimum_release_timeout}s ({math.ceil(PORT_REAP_TERM_GRACE_S)}s + "
                "execution.post_kill_cooldown_s)"
            )

    if resolved["agent"]["track"] not in VALID_TRACKS:
        raise ConfigError(
            f"agent.track must be one of {VALID_TRACKS}, got {resolved['agent']['track']!r}"
        )

    # agent.env is opaque, model-supplied configuration. It must not be able to name a variable
    # the runner owns: PORT/TM_PORT would put two workers on one simulator, CUDA_VISIBLE_DEVICES
    # would unpin the GPU, and SEED/PYTHONHASHSEED would change the protocol seed per model
    # while the result filename still claimed seed 42. Every one of those fails silently, so it
    # is an error at load time, naming the field to use instead. (The generated job script also
    # emits agent.env *before* the runner's own exports, so ordering is a second defence -- see
    # oodbench.jobscript.)
    reserved = sorted(set(resolved["agent"]["env"]) & set(RESERVED_ENV))
    if reserved:
        hints = "\n".join(f"    {k}  -> set it via {RESERVED_ENV[k]}" for k in reserved)
        raise ConfigError(
            f"agent.env may not set variable(s) the runner owns: {reserved}.\n"
            f"  The runner exports each of these itself, from validated configuration; a value "
            f"here would be fighting it for control of the ports, the GPU pinning or the "
            f"protocol seed, and every symptom of losing that fight is silent.\n"
            f"  Use the corresponding config field instead:\n{hints}"
        )

    if resolved["resume"]["mode"] not in VALID_RESUME_MODES:
        raise ConfigError(
            f"resume.mode must be one of {VALID_RESUME_MODES}, "
            f"got {resolved['resume']['mode']!r}"
        )

    for key in ("record_budget", "infra_budget", "tickruntime_budget", "killed_budget"):
        if resolved["retry"][key] < 0:
            raise ConfigError(f"retry.{key} must be >= 0")
    if resolved["retry"]["killed_budget"] < 2:
        warnings.append(
            f"retry.killed_budget={resolved['retry']['killed_budget']} buys no clean re-run: an "
            f"attempt killed while a crash-shaped record was on disk will settle on that record "
            f"immediately, even though the kill itself may have written it. 2 or more is "
            f"recommended (DESIGN.md 6A.5)."
        )
    if resolved["retry"]["worker_quarantine_after"] < 1:
        raise ConfigError("retry.worker_quarantine_after must be >= 1")

    # slurm.max_parallel is the SLURM backend's concurrency -- the number of supervision slots,
    # and the number of port pairs reserved. A value below 1 would open no slots at all and the
    # sweep would spin forever with nothing submitted.
    if resolved["slurm"]["max_parallel"] < 1:
        raise ConfigError("slurm.max_parallel must be >= 1")
    if resolved["slurm"]["submit_interval_s"] < 0:
        raise ConfigError("slurm.submit_interval_s must be >= 0")
    vulkan_index_scope = resolved["slurm"]["vulkan_index_scope"]
    if vulkan_index_scope not in VALID_VULKAN_INDEX_SCOPES:
        raise ConfigError(
            f"slurm.vulkan_index_scope must be one of {VALID_VULKAN_INDEX_SCOPES}, "
            f"got {vulkan_index_scope!r}"
        )
    if vulkan_index_scope == "allocation" and resolved["execution"]["backend"] != "slurm":
        raise ConfigError(
            "slurm.vulkan_index_scope='allocation' is valid only when "
            "execution.backend='slurm'"
        )
    if resolved["execution"]["backend"] == "slurm" and resolved["execution"]["workers"] > 1 \
            and resolved["execution"]["workers"] != resolved["slurm"]["max_parallel"]:
        warnings.append(
            f"execution.backend is 'slurm', so concurrency comes from slurm.max_parallel"
            f"={resolved['slurm']['max_parallel']}, NOT from execution.workers"
            f"={resolved['execution']['workers']}. execution.workers sizes the *local* pool and "
            f"is ignored here; set slurm.max_parallel if you meant to change how many jobs are "
            f"in flight at once."
        )

    gpus = _parse_gpus(gpus_raw, vulkan_index_scope=vulkan_index_scope)
    if any(g.vulkan_assumed for g in gpus):
        warnings.append(
            "gpus: 'vulkan' was not given for at least one GPU, so it defaults to the same "
            "index as 'cuda'. That is an assumption about this host, not a fact -- CUDA and "
            "Vulkan enumerate devices independently. Run `run_benchmark.py --check-gpus` and "
            "record the real mapping before a long multi-GPU sweep."
        )

    workers = resolved["execution"]["workers"]
    if workers > len(gpus) and not resolved["execution"]["allow_gpu_stacking"]:
        raise ConfigError(
            f"execution.workers={workers} exceeds the {len(gpus)} GPU(s) listed under `gpus`. "
            f"Two CARLA servers on one GPU contend for VRAM and throughput. Either lower "
            f"workers, list more GPUs, or set execution.allow_gpu_stacking: true to accept it."
        )
    if workers > len(gpus):
        warnings.append(
            f"execution.allow_gpu_stacking is on: {workers} workers share {len(gpus)} GPU(s). "
            f"Expect reduced throughput and elevated VRAM pressure."
        )

    # ---- existence checks (named field in the message, always) -------------------------
    _require_dir("carla.root", resolved["carla"]["root"])
    carla_sh = Path(resolved["carla"]["root"]) / "CarlaUE4.sh"
    if not carla_sh.is_file():
        raise ConfigError(
            f"carla.root={resolved['carla']['root']} does not contain CarlaUE4.sh; "
            f"point it at the root of a CARLA 0.9.15 distribution"
        )
    _require_dir("leaderboard.work_dir", resolved["leaderboard"]["work_dir"])
    _require_dir("leaderboard.root", resolved["leaderboard"]["root"])
    _require_dir("leaderboard.scenario_runner_root", resolved["leaderboard"]["scenario_runner_root"])
    evaluator = Path(resolved["leaderboard"]["root"]) / resolved["leaderboard"]["evaluator"]
    if not evaluator.is_file():
        raise ConfigError(
            f"leaderboard.evaluator resolves to {evaluator}, which does not exist"
        )
    _require_file("agent.entrypoint", resolved["agent"]["entrypoint"])
    _require_dir("routes.root", resolved["routes"]["root"])
    if resolved["routes"]["manifest"]:
        _require_file("routes.manifest", resolved["routes"]["manifest"])
    for p in resolved["agent"]["pythonpath"]:
        if not Path(p).is_dir():
            raise ConfigError(f"agent.pythonpath entry does not exist: {p}")
    if resolved["agent"]["working_dir"]:
        _require_dir("agent.working_dir", resolved["agent"]["working_dir"])

    if resolved["routes"]["strict_manifest"] and not resolved["routes"]["manifest"]:
        raise ConfigError("routes.strict_manifest is true but routes.manifest is not set")

    if resolved["benchmark"]["seed"] != 42:
        warnings.append(
            f"benchmark.seed={resolved['benchmark']['seed']} is not the published protocol seed "
            f"(42). Results will not be comparable to the released baselines. The report is "
            f"stamped protocol_seed_deviation=true."
        )

    return Config(
        benchmark=resolved["benchmark"],
        carla=resolved["carla"],
        leaderboard=resolved["leaderboard"],
        agent=resolved["agent"],
        environment=resolved["environment"],
        execution=resolved["execution"],
        ports=resolved["ports"],
        retry=resolved["retry"],
        resume=resolved["resume"],
        output=resolved["output"],
        routes=resolved["routes"],
        slurm=resolved["slurm"],
        gpus=gpus,
        source_path=source_path,
        warnings=warnings,
    )


def _require_dir(field_name: str, value: str) -> None:
    if not Path(value).is_dir():
        raise ConfigError(f"{field_name}={value} is not an existing directory")


def _require_file(field_name: str, value: str) -> None:
    if not Path(value).is_file():
        raise ConfigError(f"{field_name}={value} is not an existing file")


def load(path: str | os.PathLike, overrides: Optional[Dict[str, Any]] = None) -> Config:
    return build(load_raw(path), source_path=str(Path(path).resolve()), overrides=overrides)
