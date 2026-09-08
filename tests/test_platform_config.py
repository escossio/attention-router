import pytest
from pydantic import ValidationError

from attention_router.config import Settings


CANONICAL_PLATFORM_KEYS = {
    "health_poll_interval",
    "component_health_stale_after",
    "transport_status_stale_after",
    "database_health_stale_after",
    "runtime_provenance_stale_after",
    "readiness_result_max_age",
    "max_clock_skew",
    "execution_oneshot_lease_ttl",
    "scenario_step_lease_ttl",
    "lease_claim_max_attempts",
    "db_lock_wait_timeout",
    "default_scenario_timeout",
    "default_step_timeout",
    "external_stimulus_confirm_timeout",
    "whatsapp_response_wait_timeout",
    "max_concurrent_external_effect_scenarios",
    "max_concurrent_read_only_scenarios",
    "synthetic_test_driver_enabled",
    "max_response_chain_depth",
    "min_synthetic_stimulus_interval",
    "max_synthetic_external_runs_per_minute",
    "openai_transient_retry_max",
    "read_only_max_retries",
    "internal_idempotent_max_retries",
    "external_effect_blind_retries",
    "time_based_finding_auto_resolution",
    "default_external_effect_budget",
    "disk_warning",
    "disk_critical",
    "disk_block_new_heavy_tests",
}


def _settings(**overrides):
    return Settings(
        _env_file=None,
        admin_auth_enabled=False,
        internal_ingress_hmac_secret="synthetic-test-secret-that-is-long-enough",
        **overrides,
    )


def test_all_thirty_platform_configuration_keys_are_centralized_and_fail_closed():
    configured = set(Settings.model_fields).intersection(CANONICAL_PLATFORM_KEYS)
    assert configured == CANONICAL_PLATFORM_KEYS
    assert len(configured) == 30
    values = _settings()
    assert values.synthetic_test_driver_enabled is False
    assert values.external_effect_blind_retries == 0
    assert values.default_external_effect_budget == 0
    assert values.time_based_finding_auto_resolution is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("external_effect_blind_retries", 1),
        ("default_external_effect_budget", 1),
        ("time_based_finding_auto_resolution", True),
        ("health_poll_interval", 0),
    ],
)
def test_platform_safety_defaults_cannot_be_weakened(field, value):
    with pytest.raises(ValidationError):
        _settings(**{field: value})


def test_platform_timeout_and_disk_boundaries_are_validated():
    with pytest.raises(ValidationError):
        _settings(default_step_timeout=181)
    with pytest.raises(ValidationError):
        _settings(disk_warning=96, disk_critical=92, disk_block_new_heavy_tests=95)


@pytest.mark.parametrize(
    ("lease_field", "timeout_field"),
    [
        ("stt_processing_lease_seconds", "stt_timeout_seconds"),
        ("tts_processing_lease_seconds", "tts_timeout_seconds"),
    ],
)
def test_voice_processing_lease_must_exceed_provider_timeout(
    lease_field,
    timeout_field,
):
    with pytest.raises(ValidationError):
        _settings(**{lease_field: 30, timeout_field: 30})
    configured = _settings(**{lease_field: 31, timeout_field: 30})
    assert getattr(configured, lease_field) == 31
