import pytest

from attention_router.platform.faults import (
    FAULT_CATALOG,
    FaultInjectionDenied,
    FaultInjector,
    InjectedPlatformFault,
    validate_fault_catalog,
)


def test_all_ten_canonical_faults_are_registered():
    validate_fault_catalog()
    assert set(FAULT_CATALOG) == {f"FI-PE-{number:03d}" for number in range(1, 11)}


def test_fault_injection_is_disabled_without_explicit_safe_harness():
    with pytest.raises(FaultInjectionDenied):
        FaultInjector().arm("FI-PE-001")
    with pytest.raises(FaultInjectionDenied):
        FaultInjector(enabled=True).arm("FI-PE-001")


def test_fault_is_one_shot_and_deterministic():
    injector = FaultInjector(enabled=True, safe_environment=True)
    injector.arm("FI-PE-001")
    with pytest.raises(InjectedPlatformFault) as captured:
        injector.checkpoint("FI-PE-001")
    assert captured.value.fault_id == "FI-PE-001"
    injector.checkpoint("FI-PE-001")
