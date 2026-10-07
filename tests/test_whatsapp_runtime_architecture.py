"""The WhatsApp runtime guardrail rejects restored host-native units."""

from pathlib import Path

from scripts.check_whatsapp_runtime_architecture import check


def test_rejects_reintroduced_legacy_unit_even_when_empty(tmp_path: Path) -> None:
    package = tmp_path / "ops" / "whatsapp-container"
    package.mkdir(parents=True)
    (package / "runtime-contract.env").write_text("WHATSAPP_RUNTIME=CONTAINERIZED\n")
    (package / "compose.yaml").write_text("  browser:\n  transport:\n  observer:\n")
    (tmp_path / "README.md").write_text("candidate\n")
    assert check(tmp_path) == []

    unit_dir = tmp_path / "ops" / "systemd"
    unit_dir.mkdir()
    (unit_dir / "attention-whatsapp-browser.service").write_text("[Unit]\n")
    assert any("legacy WhatsApp unit" in error for error in check(tmp_path))
