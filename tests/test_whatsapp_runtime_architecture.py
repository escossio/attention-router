"""The WhatsApp runtime guardrail rejects restored host-native units."""

from pathlib import Path

from scripts.check_whatsapp_runtime_architecture import check


def test_rejects_reintroduced_legacy_unit_even_when_empty(tmp_path: Path) -> None:
    package = tmp_path / "ops" / "whatsapp-container"
    package.mkdir(parents=True)
    (package / "runtime-contract.env").write_text("WHATSAPP_RUNTIME=CONTAINERIZED\n")
    (package / "compose.yaml").write_text("  browser:\n  transport:\n  observer:\n")
    (tmp_path / "README.md").write_text("WHATSAPP_RUNTIME=CONTAINERIZED\n")
    assert check(tmp_path) == []

    unit_dir = tmp_path / "ops" / "systemd"
    unit_dir.mkdir()
    (unit_dir / "attention-whatsapp-browser.service").write_text("[Unit]\n")
    assert any("legacy WhatsApp unit" in error for error in check(tmp_path))


def test_rejects_host_native_runtime_declaration(tmp_path: Path) -> None:
    package = tmp_path / "ops" / "whatsapp-container"
    package.mkdir(parents=True)
    (package / "runtime-contract.env").write_text("WHATSAPP_RUNTIME=CONTAINERIZED\n")
    (package / "compose.yaml").write_text("  browser:\n  transport:\n  observer:\n")
    (tmp_path / "README.md").write_text("WHATSAPP_RUNTIME=HOST_NATIVE\n")
    assert any("README must declare" in error for error in check(tmp_path))


def test_mask_guard_can_name_retired_unit_only_in_constant() -> None:
    from scripts.check_whatsapp_runtime_architecture import (
        LEGACY_UNITS, MASK_GUARD, _without_mask_guard_list,
    )
    guarded = "LEGACY = ('attention-whatsapp-browser.service',)\n"
    assert LEGACY_UNITS.search(_without_mask_guard_list(MASK_GUARD, guarded)) is None
    unsafe = guarded + "run('systemctl', 'start', 'attention-whatsapp-browser.service')\n"
    assert LEGACY_UNITS.search(_without_mask_guard_list(MASK_GUARD, unsafe)) is not None
