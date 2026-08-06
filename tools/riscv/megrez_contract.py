"""Evidence-backed Milk-V Megrez boot contract validation."""

from __future__ import annotations

import binascii
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any


CONTRACT_PATH = Path(__file__).with_name("megrez_contract.v1.json")

_TOP_LEVEL_KEYS = {
    "schema_version",
    "contract_id",
    "claims",
    "memory",
    "handoff",
    "baseline_artifacts",
    "candidate_policy",
    "real_dtb",
    "profiles",
    "unmodelled",
    "evidence",
}
_PROVENANCE = {"observed", "derived"}
_QEMU_FIDELITY = {
    "qemu_exact",
    "qemu_envelope",
    "qemu_approximate",
    "unmodelled",
}
_HEX_RE = re.compile(r"0x(?:0|[1-9a-f][0-9a-f]*)")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CRC32_RE = re.compile(r"[0-9a-f]{8}")
_GIT_HEAD_RE = re.compile(r"[0-9a-f]{40}")
_MAX_ADDRESS_EXCLUSIVE = 1 << 64
_HASH_CHUNK_SIZE = 1024 * 1024
_FROZEN_V1_SHA256 = "a0a520b67690f91111a835eb795c0e5e21e3768551bd945454f94c82b7aae5bc"


class ContractError(ValueError):
    """The contract or a manifest violates its strict schema."""


@dataclass(frozen=True)
class ArtifactIdentity:
    """Content-derived identity for one candidate artifact."""

    size: int
    sha256: str
    crc32: str


@dataclass(frozen=True)
class CandidateManifest:
    """Identity of artifacts built from one source state."""

    source_head: str
    tracked_dirty: bool
    artifacts: Mapping[str, ArtifactIdentity]


@dataclass(frozen=True)
class MegrezContract:
    """Boot-relevant typed view over the full evidence contract."""

    raw: Mapping[str, Any]
    dram: range
    enabled_harts: tuple[int, ...]
    boot_hart: int
    mmu_mode: str
    ad_mode: str | None
    ad_alternatives: tuple[str, ...]
    board_bootargs: str
    real_dtb_runnable_under_qemu: bool


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{path} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise ContractError(f"{path} keys must be strings")
    return value


def _sequence(value: object, path: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractError(f"{path} must be an array")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], path: str) -> None:
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown:
        label = "top-level" if path == "contract" else path
        raise ContractError(f"unknown {label} fields: {sorted(unknown)}")
    if missing:
        raise ContractError(f"missing {path} fields: {sorted(missing)}")


def _integer(value: object, path: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{path} must be an integer")
    if positive and value <= 0:
        raise ContractError(f"{path} must be positive")
    return value


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{path} must be a boolean")
    return value


def _string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{path} must be a non-empty string")
    return value


def _hexadecimal(value: object, path: str) -> int:
    if not isinstance(value, str) or _HEX_RE.fullmatch(value) is None:
        raise ContractError(f"{path} must be canonical hexadecimal")
    number = int(value, 16)
    if number >= _MAX_ADDRESS_EXCLUSIVE:
        raise ContractError(f"{path} exceeds the 64-bit address space")
    return number


def _address_range(value: Mapping[str, Any], path: str) -> range:
    start = _hexadecimal(value["start"], f"{path}.start")
    end = _hexadecimal(value["end_exclusive"], f"{path}.end_exclusive")
    if start >= end:
        raise ContractError(f"{path} start must be below end_exclusive")
    return range(start, end)


def _overlap(left: range, right: range) -> bool:
    return left.start < right.stop and right.start < left.stop


def _contract_fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _deep_freeze(nested) for key, nested in value.items()}
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(_deep_freeze(nested) for nested in value)
    return value


def _validate_evidence_catalog(value: object) -> Mapping[str, Any]:
    evidence = _mapping(value, "evidence")
    if not evidence:
        raise ContractError("evidence must not be empty")
    for evidence_id, raw_record in evidence.items():
        if not evidence_id:
            raise ContractError("evidence ID must not be empty")
        record = _mapping(raw_record, f"evidence.{evidence_id}")
        allowed = {"path", "size", "sha256"}
        unknown = set(record) - allowed
        if unknown:
            raise ContractError(
                f"unknown evidence.{evidence_id} fields: {sorted(unknown)}"
            )
        if "path" not in record or "sha256" not in record:
            raise ContractError(f"evidence.{evidence_id} requires path and sha256")
        _string(record["path"], f"evidence.{evidence_id}.path")
        sha256 = _string(record["sha256"], f"evidence.{evidence_id}.sha256")
        if _SHA256_RE.fullmatch(sha256) is None:
            raise ContractError(f"evidence.{evidence_id}.sha256 is invalid")
        if "size" in record:
            _integer(record["size"], f"evidence.{evidence_id}.size", positive=True)
    return evidence


def _validate_claim_metadata(
    claim: Mapping[str, Any], path: str, evidence: Mapping[str, Any]
) -> None:
    if claim.get("provenance") not in _PROVENANCE:
        raise ContractError(f"{path}.provenance is invalid")
    if claim.get("qemu_fidelity") not in _QEMU_FIDELITY:
        raise ContractError(f"{path}.qemu_fidelity is invalid")
    references = _sequence(claim.get("evidence"), f"{path}.evidence")
    if not references:
        raise ContractError(f"{path}.evidence must not be empty")
    for reference in references:
        if not isinstance(reference, str) or reference not in evidence:
            raise ContractError(f"{path}.evidence contains an unknown ID")


def _validate_claims(
    value: object, evidence: Mapping[str, Any]
) -> tuple[
    tuple[int, ...],
    int,
    str,
    str | None,
    tuple[str, ...],
    str,
    tuple[str, ...],
]:
    claims = _mapping(value, "claims")
    _exact_keys(claims, {"identity", "cpu", "timebase", "mmu", "ad_mode"}, "claims")
    schemas = {
        "identity": {"model", "soc", "compatible"},
        "cpu": {"enabled_harts", "boot_hart"},
        "timebase": {"hz"},
        "mmu": {"value"},
        "ad_mode": {"value", "alternatives"},
    }
    metadata = {"provenance", "qemu_fidelity", "evidence"}
    for name, fields in schemas.items():
        claim = _mapping(claims[name], f"claims.{name}")
        _exact_keys(claim, fields | metadata, f"claims.{name}")
        _validate_claim_metadata(claim, f"claims.{name}", evidence)

    identity = _mapping(claims["identity"], "claims.identity")
    for field in ("model", "soc"):
        _string(identity[field], f"claims.identity.{field}")
    compatible = tuple(
        _string(item, "claims.identity.compatible item")
        for item in _sequence(
            identity["compatible"], "claims.identity.compatible"
        )
    )
    if (
        identity["model"] != "Milk-V Megrez"
        or identity["soc"] != "ESWIN EIC7700"
        or compatible
        != (
            "sifive,hifive-unmatched-a00",
            "sifive,fu740-c000",
            "sifive,fu740",
            "eswin,eic7700",
        )
    ):
        raise ContractError("claims.identity disagrees with the Megrez contract")

    cpu = _mapping(claims["cpu"], "claims.cpu")
    hart_values = _sequence(cpu["enabled_harts"], "claims.cpu.enabled_harts")
    enabled_harts = tuple(
        _integer(hart, "claims.cpu.enabled_harts item") for hart in hart_values
    )
    if enabled_harts != tuple(sorted(set(enabled_harts))) or not enabled_harts:
        raise ContractError("claims.cpu.enabled_harts must be unique and sorted")
    boot_hart = _integer(cpu["boot_hart"], "claims.cpu.boot_hart")
    if boot_hart not in enabled_harts:
        raise ContractError("claims.cpu.boot_hart must be enabled")

    timebase = _mapping(claims["timebase"], "claims.timebase")
    _integer(timebase["hz"], "claims.timebase.hz", positive=True)

    mmu = _mapping(claims["mmu"], "claims.mmu")
    mmu_mode = _string(mmu["value"], "claims.mmu.value")
    if mmu_mode != "riscv,sv48":
        raise ContractError("claims.mmu.value must be riscv,sv48")

    ad_mode = _mapping(claims["ad_mode"], "claims.ad_mode")
    alternatives = tuple(
        _sequence(ad_mode["alternatives"], "claims.ad_mode.alternatives")
    )
    if (
        ad_mode["value"] is not None
        or alternatives != ("svade", "svadu")
        or ad_mode["qemu_fidelity"] != "qemu_envelope"
    ):
        raise ContractError("A/D mode must remain unknown with a Svade/Svadu envelope")
    return (
        enabled_harts,
        boot_hart,
        mmu_mode,
        None,
        alternatives,
        identity["model"],
        compatible,
    )


def _validate_memory(
    value: object, evidence: Mapping[str, Any]
) -> tuple[range, tuple[tuple[str, range], ...]]:
    memory = _mapping(value, "memory")
    _exact_keys(memory, {"dram", "fixed_no_map", "dynamic_cma"}, "memory")
    metadata = {"provenance", "qemu_fidelity", "evidence"}

    dram_data = _mapping(memory["dram"], "memory.dram")
    _exact_keys(dram_data, {"start", "end_exclusive"} | metadata, "memory.dram")
    _validate_claim_metadata(dram_data, "memory.dram", evidence)
    dram = _address_range(dram_data, "memory.dram")

    fixed_data = _sequence(memory["fixed_no_map"], "memory.fixed_no_map")
    fixed: list[tuple[str, range]] = []
    for index, raw_item in enumerate(fixed_data):
        path = f"memory.fixed_no_map[{index}]"
        item = _mapping(raw_item, path)
        _exact_keys(item, {"name", "start", "end_exclusive"} | metadata, path)
        _validate_claim_metadata(item, path, evidence)
        name = _string(item["name"], f"{path}.name")
        fixed.append((name, _address_range(item, path)))
    if len({name for name, _ in fixed}) != len(fixed):
        raise ContractError("fixed no-map names must be unique")
    for index, (left_name, left) in enumerate(fixed):
        for right_name, right in fixed[index + 1 :]:
            if _overlap(left, right):
                raise ContractError(
                    f"fixed no-map ranges overlap: {left_name} and {right_name}"
                )

    cma = _mapping(memory["dynamic_cma"], "memory.dynamic_cma")
    _exact_keys(
        cma,
        {"size", "alignment", "alloc_start", "alloc_end_exclusive"} | metadata,
        "memory.dynamic_cma",
    )
    _validate_claim_metadata(cma, "memory.dynamic_cma", evidence)
    size = _hexadecimal(cma["size"], "memory.dynamic_cma.size")
    alignment = _hexadecimal(cma["alignment"], "memory.dynamic_cma.alignment")
    alloc_start = _hexadecimal(cma["alloc_start"], "memory.dynamic_cma.alloc_start")
    alloc_end = _hexadecimal(
        cma["alloc_end_exclusive"], "memory.dynamic_cma.alloc_end_exclusive"
    )
    if size == 0 or alignment == 0 or alloc_start >= alloc_end:
        raise ContractError("memory.dynamic_cma values are invalid")
    if alloc_start < dram.start or alloc_end > dram.stop:
        raise ContractError("memory.dynamic_cma allocation is outside DRAM")
    if alignment & (alignment - 1):
        raise ContractError("memory.dynamic_cma alignment must be a power of two")
    if size > alloc_end - alloc_start:
        raise ContractError("memory.dynamic_cma size exceeds its allocation window")
    if size % alignment:
        raise ContractError("memory.dynamic_cma size is not alignment-compatible")
    if alloc_start % alignment or alloc_end % alignment:
        raise ContractError("memory.dynamic_cma allocation range is not aligned")
    return dram, tuple(fixed)


def _validate_baseline_artifacts(
    value: object, evidence: Mapping[str, Any]
) -> Mapping[str, Mapping[str, Any]]:
    artifacts = _mapping(value, "baseline_artifacts")
    _exact_keys(artifacts, {"kernel", "initramfs", "dtb"}, "baseline_artifacts")
    metadata = {"provenance", "qemu_fidelity", "evidence"}
    for name, raw_identity in artifacts.items():
        identity = _mapping(raw_identity, f"baseline_artifacts.{name}")
        _exact_keys(
            identity,
            {"size", "sha256", "crc32"} | metadata,
            f"baseline_artifacts.{name}",
        )
        _validate_claim_metadata(identity, f"baseline_artifacts.{name}", evidence)
        _integer(identity["size"], f"baseline_artifacts.{name}.size", positive=True)
        sha256 = identity["sha256"]
        if name == "dtb":
            if sha256 is not None:
                raise ContractError("baseline_artifacts.dtb.sha256 must remain unknown")
        elif not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
            raise ContractError(f"baseline_artifacts.{name}.sha256 is invalid")
        crc32 = identity["crc32"]
        if not isinstance(crc32, str) or _CRC32_RE.fullmatch(crc32) is None:
            raise ContractError(f"baseline_artifacts.{name}.crc32 is invalid")
    return artifacts


def _validate_handoff(
    value: object,
    evidence: Mapping[str, Any],
    artifacts: Mapping[str, Mapping[str, Any]],
    dram: range,
    fixed: tuple[tuple[str, range], ...],
) -> str:
    handoff = _mapping(value, "handoff")
    _exact_keys(handoff, {"kernel", "initramfs", "dtb", "bootargs"}, "handoff")
    metadata = {"provenance", "qemu_fidelity", "evidence"}
    payloads: list[tuple[str, range]] = []
    for name in ("kernel", "initramfs", "dtb"):
        item = _mapping(handoff[name], f"handoff.{name}")
        _exact_keys(item, {"address", "artifact"} | metadata, f"handoff.{name}")
        _validate_claim_metadata(item, f"handoff.{name}", evidence)
        artifact_name = _string(item["artifact"], f"handoff.{name}.artifact")
        if artifact_name != name:
            raise ContractError(f"handoff.{name}.artifact must be {name}")
        start = _hexadecimal(item["address"], f"handoff.{name}.address")
        size = _integer(artifacts[name]["size"], f"baseline_artifacts.{name}.size")
        payload = range(start, start + size)
        if payload.start < dram.start or payload.stop > dram.stop:
            raise ContractError(f"{name} is outside DRAM")
        for fixed_name, fixed_range in fixed:
            if _overlap(payload, fixed_range):
                raise ContractError(f"{name} overlaps fixed no-map range {fixed_name}")
        payloads.append((name, payload))
    for index, (left_name, left) in enumerate(payloads):
        for right_name, right in payloads[index + 1 :]:
            if _overlap(left, right):
                raise ContractError(
                    f"payload ranges overlap: {left_name} and {right_name}"
                )

    bootargs = _mapping(handoff["bootargs"], "handoff.bootargs")
    _exact_keys(
        bootargs,
        {"value", "persistent_write_forbidden"} | metadata,
        "handoff.bootargs",
    )
    _validate_claim_metadata(bootargs, "handoff.bootargs", evidence)
    _boolean(
        bootargs["persistent_write_forbidden"],
        "handoff.bootargs.persistent_write_forbidden",
    )
    if bootargs["persistent_write_forbidden"] is not True:
        raise ContractError("handoff.bootargs must forbid persistent writes")
    return _string(bootargs["value"], "handoff.bootargs.value")


def _validate_candidate_policy(value: object) -> None:
    policy = _mapping(value, "candidate_policy")
    expected = {
        "source",
        "calculate_identity_per_run",
        "record_dirty_state",
        "baseline_match_required",
    }
    _exact_keys(policy, expected, "candidate_policy")
    if policy["source"] != "current_git_head":
        raise ContractError("candidate_policy.source must be current_git_head")
    for field in expected - {"source"}:
        _boolean(policy[field], f"candidate_policy.{field}")
    if (
        policy["calculate_identity_per_run"] is not True
        or policy["record_dirty_state"] is not True
        or policy["baseline_match_required"] is not False
    ):
        raise ContractError("candidate_policy does not preserve candidate identity")


def _validate_real_dtb(
    value: object,
    evidence: Mapping[str, Any],
    *,
    dram: range,
    enabled_harts: tuple[int, ...],
    mmu_mode: str,
    model: str,
    compatible: tuple[str, ...],
    artifacts: Mapping[str, Mapping[str, Any]],
) -> bool:
    dtb = _mapping(value, "real_dtb")
    expected = {
        "model",
        "compatible",
        "enabled_cpu_count",
        "mmu_type",
        "memory_start",
        "memory_end_exclusive",
        "size",
        "crc32",
        "sha256",
        "identity_strength",
        "rng_seed_present",
        "runnable_under_qemu",
        "provenance",
        "qemu_fidelity",
        "evidence",
    }
    _exact_keys(dtb, expected, "real_dtb")
    _validate_claim_metadata(dtb, "real_dtb", evidence)
    for field in ("model", "mmu_type", "identity_strength"):
        _string(dtb[field], f"real_dtb.{field}")
    dtb_compatible = tuple(
        _string(item, "real_dtb.compatible item")
        for item in _sequence(dtb["compatible"], "real_dtb.compatible")
    )
    enabled_cpu_count = _integer(
        dtb["enabled_cpu_count"], "real_dtb.enabled_cpu_count", positive=True
    )
    size = _integer(dtb["size"], "real_dtb.size", positive=True)
    memory_start = _hexadecimal(dtb["memory_start"], "real_dtb.memory_start")
    memory_end = _hexadecimal(
        dtb["memory_end_exclusive"], "real_dtb.memory_end_exclusive"
    )
    if not isinstance(dtb["crc32"], str) or _CRC32_RE.fullmatch(dtb["crc32"]) is None:
        raise ContractError("real_dtb.crc32 is invalid")
    if dtb["sha256"] is not None or dtb["identity_strength"] != "size_crc":
        raise ContractError("real_dtb identity must remain size_crc without SHA-256")
    _boolean(dtb["rng_seed_present"], "real_dtb.rng_seed_present")
    runnable = _boolean(dtb["runnable_under_qemu"], "real_dtb.runnable_under_qemu")
    if runnable:
        raise ContractError("real_dtb must not be runnable under QEMU")
    if dtb["model"] != model or dtb_compatible != compatible:
        raise ContractError("real_dtb identity disagrees with board evidence")
    if enabled_cpu_count != len(enabled_harts):
        raise ContractError("real_dtb enabled CPU count disagrees with board evidence")
    if dtb["mmu_type"] != mmu_mode:
        raise ContractError("real_dtb MMU mode disagrees with board evidence")
    if (memory_start, memory_end) != (dram.start, dram.stop):
        raise ContractError("real_dtb memory range disagrees with board evidence")
    baseline_dtb = artifacts["dtb"]
    if size != baseline_dtb["size"] or dtb["crc32"] != baseline_dtb["crc32"]:
        raise ContractError("real_dtb size or CRC disagrees with board evidence")
    if dtb["rng_seed_present"]:
        raise ContractError("real_dtb unexpectedly claims an rng-seed")
    return runnable


def _validate_profiles(value: object, board_bootargs: str) -> None:
    profiles = _mapping(value, "profiles")
    expected_names = {
        "megrez-sv48-svade-fast",
        "megrez-sv48-svadu-fast",
        "megrez-sv48-slow",
    }
    _exact_keys(profiles, expected_names, "profiles")
    fields = {
        "memory",
        "hart_count",
        "mmu_type",
        "ad_extension",
        "zkr",
        "svpbmt",
        "bootargs",
        "remove_rng_seed",
        "resource_gate",
    }
    for name, raw_profile in profiles.items():
        profile = _mapping(raw_profile, f"profiles.{name}")
        _exact_keys(profile, fields, f"profiles.{name}")
        memory = _hexadecimal(profile["memory"], f"profiles.{name}.memory")
        expected_memory = 0x400000000 if name.endswith("-slow") else 0x80000000
        if memory != expected_memory:
            raise ContractError(f"profile {name} has the wrong memory size")
        if _integer(profile["hart_count"], f"profiles.{name}.hart_count") != 4:
            raise ContractError(f"profiles.{name} must use four harts")
        if profile["mmu_type"] != "riscv,sv48":
            raise ContractError(f"profiles.{name} must use Sv48")
        expected_ad = "svadu" if "svadu" in name else "svade"
        if profile["ad_extension"] != expected_ad:
            raise ContractError(f"profiles.{name} has the wrong A/D extension")
        for field in ("zkr", "svpbmt", "remove_rng_seed", "resource_gate"):
            _boolean(profile[field], f"profiles.{name}.{field}")
        if profile["zkr"] or profile["svpbmt"] or not profile["remove_rng_seed"]:
            raise ContractError(f"profiles.{name} requires unproven extensions or seed")
        _string(profile["bootargs"], f"profiles.{name}.bootargs")
        if profile["bootargs"] != board_bootargs:
            raise ContractError(f"profile {name} does not use exact board bootargs")
        if name.endswith("-slow") != profile["resource_gate"]:
            raise ContractError(f"profiles.{name} has an invalid resource gate")


def validate_contract(data: Mapping[str, object]) -> MegrezContract:
    """Validate a complete v1 contract and return its typed boot view."""

    contract = _mapping(data, "contract")
    _exact_keys(contract, _TOP_LEVEL_KEYS, "contract")
    version = _integer(contract["schema_version"], "schema_version")
    if version != 1:
        raise ContractError("schema_version must be 1")
    if contract["contract_id"] != "milkv-megrez-eic7700":
        raise ContractError("contract_id is invalid")

    evidence = _validate_evidence_catalog(contract["evidence"])
    (
        enabled_harts,
        boot_hart,
        mmu_mode,
        ad_mode,
        alternatives,
        model,
        compatible,
    ) = _validate_claims(contract["claims"], evidence)
    dram, fixed = _validate_memory(contract["memory"], evidence)
    artifacts = _validate_baseline_artifacts(contract["baseline_artifacts"], evidence)
    board_bootargs = _validate_handoff(
        contract["handoff"], evidence, artifacts, dram, fixed
    )
    _validate_candidate_policy(contract["candidate_policy"])
    runnable = _validate_real_dtb(
        contract["real_dtb"],
        evidence,
        dram=dram,
        enabled_harts=enabled_harts,
        mmu_mode=mmu_mode,
        model=model,
        compatible=compatible,
        artifacts=artifacts,
    )
    _validate_profiles(contract["profiles"], board_bootargs)

    unmodelled = _sequence(contract["unmodelled"], "unmodelled")
    if not unmodelled or any(
        not isinstance(item, str) or not item for item in unmodelled
    ):
        raise ContractError("unmodelled must contain non-empty strings")
    if len(set(unmodelled)) != len(unmodelled):
        raise ContractError("unmodelled entries must be unique")
    if _contract_fingerprint(contract) != _FROZEN_V1_SHA256:
        raise ContractError("contract facts differ from the reviewed frozen v1")

    frozen_contract = _deep_freeze(contract)

    return MegrezContract(
        raw=frozen_contract,
        dram=dram,
        enabled_harts=enabled_harts,
        boot_hart=boot_hart,
        mmu_mode=mmu_mode,
        ad_mode=ad_mode,
        ad_alternatives=alternatives,
        board_bootargs=board_bootargs,
        real_dtb_runnable_under_qemu=runnable,
    )


def load_contract(path: Path = CONTRACT_PATH) -> MegrezContract:
    """Load and validate a Megrez v1 JSON contract."""

    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"cannot load contract {path}: {error}") from error
    return validate_contract(data)


def artifact_identity(path: Path) -> ArtifactIdentity:
    """Calculate the identity of exactly the bytes at ``path``."""

    size = 0
    sha256 = hashlib.sha256()
    crc32 = 0
    with path.open("rb") as artifact:
        while chunk := artifact.read(_HASH_CHUNK_SIZE):
            size += len(chunk)
            sha256.update(chunk)
            crc32 = binascii.crc32(chunk, crc32)
    return ArtifactIdentity(
        size=size,
        sha256=sha256.hexdigest(),
        crc32=f"{crc32 & 0xFFFF_FFFF:08x}",
    )


def validate_candidate_manifest(data: Mapping[str, object]) -> CandidateManifest:
    """Validate per-run candidate identity without comparing it to the baseline."""

    manifest = _mapping(data, "candidate_manifest")
    _exact_keys(
        manifest,
        {"source_head", "tracked_dirty", "artifacts"},
        "candidate_manifest",
    )
    source_head = manifest["source_head"]
    if not isinstance(source_head, str) or _GIT_HEAD_RE.fullmatch(source_head) is None:
        raise ContractError("candidate_manifest.source_head must be a Git SHA-1")
    tracked_dirty = _boolean(
        manifest["tracked_dirty"], "candidate_manifest.tracked_dirty"
    )
    raw_artifacts = _mapping(manifest["artifacts"], "candidate_manifest.artifacts")
    if not raw_artifacts:
        raise ContractError("candidate_manifest.artifacts must not be empty")
    allowed_artifacts = {"kernel", "image", "elf", "initramfs", "dtb"}
    unknown = set(raw_artifacts) - allowed_artifacts
    if unknown:
        raise ContractError(f"unknown candidate artifacts: {sorted(unknown)}")
    artifacts: dict[str, ArtifactIdentity] = {}
    for name, raw_identity in raw_artifacts.items():
        identity = _mapping(raw_identity, f"candidate_manifest.artifacts.{name}")
        _exact_keys(
            identity,
            {"size", "sha256", "crc32"},
            f"candidate_manifest.artifacts.{name}",
        )
        size = _integer(
            identity["size"], f"candidate_manifest.artifacts.{name}.size", positive=True
        )
        sha256 = identity["sha256"]
        crc32 = identity["crc32"]
        if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
            raise ContractError(
                f"candidate_manifest.artifacts.{name}.sha256 is invalid"
            )
        if not isinstance(crc32, str) or _CRC32_RE.fullmatch(crc32) is None:
            raise ContractError(f"candidate_manifest.artifacts.{name}.crc32 is invalid")
        artifacts[name] = ArtifactIdentity(size=size, sha256=sha256, crc32=crc32)
    return CandidateManifest(
        source_head=source_head,
        tracked_dirty=tracked_dirty,
        artifacts=MappingProxyType(artifacts),
    )
