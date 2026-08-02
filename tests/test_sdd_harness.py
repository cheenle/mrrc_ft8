"""Regression tests for the project SDD harness."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


HARNESS_PATH = (
    Path(__file__).parents[1]
    / ".agents"
    / "skills"
    / "sdd-guardian"
    / "harness"
    / "sdd_context.py"
)
SPEC = spec_from_file_location("sdd_context", HARNESS_PATH)
assert SPEC is not None and SPEC.loader is not None
SDD_CONTEXT = module_from_spec(SPEC)
SPEC.loader.exec_module(SDD_CONTEXT)


def test_trace_recognizes_multi_digit_sdd_identifiers() -> None:
    """Trace citations must preserve full two-digit risk and criterion IDs."""
    refs = SDD_CONTEXT._refs_present("SC10 R10 I11 A6 SC2 R1")

    assert {"SC10", "R10", "I11", "A6", "SC2", "R1"} <= refs


def test_task5_quality_fix_records_are_synchronized() -> None:
    root = Path(__file__).parents[1]
    architecture = (root / "SDD/09-architecture-overview.md").read_text()
    components = (root / "SDD/11-component-model.md").read_text()
    feasibility = (root / "SDD/13-feasibility-assessment.md").read_text()
    history = (root / "SDD/14-version-history.md").read_text()
    agents = (root / "AGENTS.md").read_text()
    inventory = (root / "tests/README.md").read_text()
    plan = (
        root
        / "docs/superpowers/plans/2026-08-01-ft8-dsp-worker.md"
    ).read_text()
    patched_copies = (
        "encode174_91var.f90",
        "osd174_91var.f90",
        "four2avar.f90",
        "ft8_mod1.f90",
        "ft8_decodevar.f90",
        "ft8_downsamplevar.f90",
        "ft8apsetvar.f90",
    )

    for name in patched_copies:
        assert name in components
        assert name in agents
        assert name in plan
    for phrase in (
        "band-local subtraction",
        "WSJT_E_INTERNAL=8",
        "result order is unspecified",
        "process exit",
    ):
        assert phrase in architecture or phrase in components
    assert "weak direct-A8 fixture" in feasibility
    assert "weak direct-A8 fixture" in inventory
    assert "## Unreleased" in history
    assert "| SDD version | V1.0 |" in (root / "SDD/README.md").read_text()
