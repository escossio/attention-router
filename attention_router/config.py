from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Attention Router"
    app_env: str = "production"
    http_host: str = "0.0.0.0"
    http_port: int = 18100
    database_url: str = "sqlite+pysqlite:///:memory:"
    worker_poll_interval_seconds: int = 2
    worker_timer_lease_seconds: int = 60
    default_ack_timeout_seconds: int = 300
    admin_auth_enabled: bool = True
    admin_token: str | None = None
    ingress_http_host: str = "0.0.0.0"
    ingress_http_port: int = 18101
    internal_ingress_http_host: str = "0.0.0.0"
    internal_ingress_http_port: int = 18102
    internal_ingress_hmac_secret: str | None = None
    internal_ingress_max_skew_seconds: int = 300
    internal_ingress_max_body_bytes: int = 64 * 1024
    internal_ingress_source: str = "wwebjs"
    meta_whatsapp_enabled: bool = False
    meta_webhook_dispatch_enabled: bool = False
    meta_human_auth_control_enabled: bool = False
    meta_verify_token: str | None = None
    meta_app_secret: str | None = None
    meta_app_id: str | None = None
    meta_waba_id: str | None = None
    meta_phone_number_id: str | None = None
    meta_graph_api_version: str = "v26.0"
    meta_webhook_public_hostname: str | None = None
    meta_max_webhook_body_bytes: int = 256 * 1024
    meta_admission_grace_seconds: int = 30
    meta_admission_transaction_timeout_seconds: int = 10
    meta_reconciliation_batch_size: int = 100
    meta_reconciliation_transaction_timeout_seconds: int = 15
    meta_access_token: str | None = None
    meta_whatsapp_access_token: str | None = None
    meta_whatsapp_phone_number_id: str | None = None
    meta_whatsapp_waba_id: str | None = None
    wwebjs_outbound_url: str = "http://192.0.2.6:18103/internal/send"
    wwebjs_outbound_hmac_secret: str | None = None
    wwebjs_outbound_timeout_seconds: float = 5.0
    attention_recent_window_seconds: int = 10 * 60
    attention_rapid_repeat_seconds: int = 90
    attention_persistent_message_count: int = 3
    attention_short_text_max_chars: int = 80
    attention_medium_text_max_chars: int = 240
    agent_builder_interviewer_provider: str = "deterministic"
    agent_builder_openai_model: str = "gpt-5-mini"
    agent_builder_ai_timeout_seconds: float = 20.0
    openai_api_key: str | None = None
    tts_enabled: bool = False
    tts_profile: str = "andy"
    tts_auto_send: bool = False
    tts_internal_url: str = "http://tts-api-switcher:8090/internal/tts"
    tts_internal_token: str | None = None
    tts_timeout_seconds: float = 20.0
    tts_processing_lease_seconds: int = 120
    tts_max_response_bytes: int = 5 * 1024 * 1024
    whatsapp_media_root: str = "/var/lib/attention-router/whatsapp-media"
    whatsapp_media_max_bytes: int = 5 * 1024 * 1024
    media_retention_hours: int = 24
    media_ambiguous_retention_hours: int = 168
    stt_enabled: bool = False
    stt_internal_url: str = "http://tts-api-switcher:8090/internal/stt"
    stt_internal_token: str | None = None
    stt_timeout_seconds: float = 30.0
    stt_processing_lease_seconds: int = 120
    agent_decision_pipeline_enabled: bool = True
    agent_decision_pipeline_version: str = "v1"
    agent_decision_default_blueprint_id: str | None = None
    andy_behavior_enabled: bool = False
    andy_behavior_canary_binding_id: str | None = None
    andy_agent_enabled: bool = False
    andy_agent_model: str = "gpt-5.6-sol"
    andy_agent_timeout_seconds: float = 20.0
    andy_agent_max_turns: int = 3
    andy_behavior_profile_path: str = "config/andy_behavior_profile.json"
    legacy_external_fallback_enabled: bool = False
    persistent_memory_enabled: bool = False
    memory_ingestion_enabled: bool = False
    memory_context_enabled: bool = False
    persistent_memory_canary_binding_id: str | None = None
    conversation_repetition_window_seconds: int = 24 * 60 * 60
    agent_response_review_enabled: bool = True
    agent_execution_enabled: bool = True
    external_delivery_enabled: bool = False
    autonomous_execution_enabled: bool = False
    autonomous_execution_activated_at: str | None = None
    autonomous_decision_max_age_seconds: int = 300
    local_transport_outbound_url: str = "http://192.0.2.6:18103/internal/send"
    local_transport_outbound_timeout_seconds: float = 5.0
    synthetic_transport_status_url: str | None = None
    synthetic_transport_status_timeout_seconds: float = 5.0
    otel_tracing_enabled: bool = False
    otel_service_name: str = "attention-router-api"
    otel_service_version: str = "0.1.0"
    otel_exporter_otlp_endpoint: str | None = None
    otel_exporter_otlp_protocol: str = "http/protobuf"
    otel_resource_attributes: str | None = None
    otel_traces_sampler: str = "parentbased_traceidratio"
    otel_traces_sampler_arg: float = 1.0
    otel_batch_max_queue_size: int = 2048
    otel_batch_max_export_batch_size: int = 512
    otel_batch_schedule_delay_millis: int = 500
    otel_export_timeout_millis: int = 5000

    # Platform Evolution controls. Environment names are the upper-case field names.
    health_poll_interval: int = 10
    component_health_stale_after: int = 30
    transport_status_stale_after: int = 30
    database_health_stale_after: int = 30
    runtime_provenance_stale_after: int = 300
    readiness_result_max_age: int = 15
    synthetic_l1_bounded_auth_validity_seconds: int = 900
    max_clock_skew: int = 5
    execution_oneshot_lease_ttl: int = 120
    scenario_step_lease_ttl: int = 180
    lease_claim_max_attempts: int = 3
    db_lock_wait_timeout: int = 3
    default_scenario_timeout: int = 180
    default_step_timeout: int = 60
    external_stimulus_confirm_timeout: int = 30
    whatsapp_response_wait_timeout: int = 90
    max_concurrent_external_effect_scenarios: int = 1
    max_concurrent_read_only_scenarios: int = 4
    synthetic_test_driver_enabled: bool = False
    max_response_chain_depth: int = 1
    min_synthetic_stimulus_interval: int = 5
    max_synthetic_external_runs_per_minute: int = 2
    openai_transient_retry_max: int = 1
    read_only_max_retries: int = 2
    internal_idempotent_max_retries: int = 2
    external_effect_blind_retries: int = 0
    time_based_finding_auto_resolution: bool = False
    default_external_effect_budget: int = 0
    disk_warning: int = 85
    disk_critical: int = 92
    disk_block_new_heavy_tests: int = 95

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @model_validator(mode="after")
    def validate_environment(self) -> "Settings":
        allowed = {"test", "development", "production", "private"}
        if self.app_env not in allowed:
            raise ValueError(f"APP_ENV must be one of {sorted(allowed)}")
        if self.worker_poll_interval_seconds <= 0:
            raise ValueError("WORKER_POLL_INTERVAL_SECONDS must be positive")
        if self.worker_timer_lease_seconds <= 0:
            raise ValueError("WORKER_TIMER_LEASE_SECONDS must be positive")
        if self.default_ack_timeout_seconds < 60 and self.app_env == "production":
            raise ValueError("production DEFAULT_ACK_TIMEOUT_SECONDS must be at least 60")
        if self.admin_auth_enabled and not self.admin_token:
            raise ValueError("ADMIN_TOKEN is required when ADMIN_AUTH_ENABLED=true")
        if self.admin_auth_enabled and self.admin_token and len(self.admin_token) < 16:
            raise ValueError("ADMIN_TOKEN must have at least 16 characters")
        if self.ingress_http_port <= 0:
            raise ValueError("INGRESS_HTTP_PORT must be positive")
        if self.internal_ingress_http_port <= 0:
            raise ValueError("INTERNAL_INGRESS_HTTP_PORT must be positive")
        if self.internal_ingress_max_skew_seconds <= 0:
            raise ValueError("INTERNAL_INGRESS_MAX_SKEW_SECONDS must be positive")
        if self.internal_ingress_max_body_bytes <= 0:
            raise ValueError("INTERNAL_INGRESS_MAX_BODY_BYTES must be positive")
        if not self.internal_ingress_hmac_secret:
            raise ValueError("INTERNAL_INGRESS_HMAC_SECRET is required")
        if len(self.internal_ingress_hmac_secret) < 32:
            raise ValueError("INTERNAL_INGRESS_HMAC_SECRET must have at least 32 characters")
        if self.meta_max_webhook_body_bytes <= 0:
            raise ValueError("META_MAX_WEBHOOK_BODY_BYTES must be positive")
        if self.meta_admission_grace_seconds <= 0:
            raise ValueError("META_ADMISSION_GRACE_SECONDS must be positive")
        if not (
            0
            < self.meta_admission_transaction_timeout_seconds
            < self.meta_admission_grace_seconds
        ):
            raise ValueError(
                "META_ADMISSION_TRANSACTION_TIMEOUT_SECONDS must be inside grace"
            )
        if not 0 < self.meta_reconciliation_batch_size <= 100:
            raise ValueError("META_RECONCILIATION_BATCH_SIZE must be between 1 and 100")
        if self.meta_reconciliation_transaction_timeout_seconds <= 0:
            raise ValueError(
                "META_RECONCILIATION_TRANSACTION_TIMEOUT_SECONDS must be positive"
            )
        if self.meta_whatsapp_enabled:
            if not self.meta_verify_token:
                raise ValueError("META_VERIFY_TOKEN is required when META_WHATSAPP_ENABLED=true")
            if not self.meta_app_secret:
                raise ValueError("META_APP_SECRET is required when META_WHATSAPP_ENABLED=true")
        if self.wwebjs_outbound_hmac_secret and len(self.wwebjs_outbound_hmac_secret) < 32:
            raise ValueError("WWEBJS_OUTBOUND_HMAC_SECRET must have at least 32 characters")
        if self.wwebjs_outbound_timeout_seconds <= 0:
            raise ValueError("WWEBJS_OUTBOUND_TIMEOUT_SECONDS must be positive")
        if self.attention_recent_window_seconds <= 0:
            raise ValueError("ATTENTION_RECENT_WINDOW_SECONDS must be positive")
        if self.attention_rapid_repeat_seconds <= 0:
            raise ValueError("ATTENTION_RAPID_REPEAT_SECONDS must be positive")
        if self.attention_persistent_message_count < 2:
            raise ValueError("ATTENTION_PERSISTENT_MESSAGE_COUNT must be at least 2")
        if self.attention_short_text_max_chars <= 0:
            raise ValueError("ATTENTION_SHORT_TEXT_MAX_CHARS must be positive")
        if self.attention_medium_text_max_chars < self.attention_short_text_max_chars:
            raise ValueError("ATTENTION_MEDIUM_TEXT_MAX_CHARS must be >= ATTENTION_SHORT_TEXT_MAX_CHARS")
        if self.agent_builder_interviewer_provider not in {"deterministic", "openai"}:
            raise ValueError("AGENT_BUILDER_INTERVIEWER_PROVIDER must be deterministic or openai")
        if self.agent_builder_ai_timeout_seconds <= 0:
            raise ValueError("AGENT_BUILDER_AI_TIMEOUT_SECONDS must be positive")
        if self.legacy_external_fallback_enabled and self.app_env not in {"test", "development"}:
            raise ValueError("LEGACY_EXTERNAL_FALLBACK_ENABLED is test/development only")
        if self.andy_agent_timeout_seconds <= 0 or self.andy_agent_max_turns <= 0:
            raise ValueError("ANDY_AGENT limits must be positive")
        if self.tts_profile != "andy":
            raise ValueError("TTS_PROFILE must be andy")
        if self.tts_enabled and not self.tts_internal_token:
            raise ValueError("TTS_INTERNAL_TOKEN is required when TTS_ENABLED=true")
        if self.tts_timeout_seconds <= 0:
            raise ValueError("TTS_TIMEOUT_SECONDS must be positive")
        if self.tts_processing_lease_seconds <= self.tts_timeout_seconds:
            raise ValueError("TTS_PROCESSING_LEASE_SECONDS must be greater than TTS_TIMEOUT_SECONDS")
        if self.tts_max_response_bytes <= 0:
            raise ValueError("TTS_MAX_RESPONSE_BYTES must be positive")
        if self.whatsapp_media_max_bytes <= 0:
            raise ValueError("WHATSAPP_MEDIA_MAX_BYTES must be positive")
        if self.media_retention_hours <= 0 or self.media_ambiguous_retention_hours <= 0:
            raise ValueError("MEDIA retention values must be positive")
        if self.stt_enabled and not self.stt_internal_token:
            raise ValueError("STT_INTERNAL_TOKEN is required when STT_ENABLED=true")
        if self.stt_timeout_seconds <= 0:
            raise ValueError("STT_TIMEOUT_SECONDS must be positive")
        if self.stt_processing_lease_seconds <= self.stt_timeout_seconds:
            raise ValueError("STT_PROCESSING_LEASE_SECONDS must be greater than STT_TIMEOUT_SECONDS")
        if self.autonomous_decision_max_age_seconds <= 0:
            raise ValueError("AUTONOMOUS_DECISION_MAX_AGE_SECONDS must be positive")
        if self.conversation_repetition_window_seconds <= 0:
            raise ValueError("CONVERSATION_REPETITION_WINDOW_SECONDS must be positive")
        positive_platform_limits = {
            "HEALTH_POLL_INTERVAL": self.health_poll_interval,
            "COMPONENT_HEALTH_STALE_AFTER": self.component_health_stale_after,
            "TRANSPORT_STATUS_STALE_AFTER": self.transport_status_stale_after,
            "DATABASE_HEALTH_STALE_AFTER": self.database_health_stale_after,
            "RUNTIME_PROVENANCE_STALE_AFTER": self.runtime_provenance_stale_after,
            "READINESS_RESULT_MAX_AGE": self.readiness_result_max_age,
            "MAX_CLOCK_SKEW": self.max_clock_skew,
            "EXECUTION_ONESHOT_LEASE_TTL": self.execution_oneshot_lease_ttl,
            "SCENARIO_STEP_LEASE_TTL": self.scenario_step_lease_ttl,
            "LEASE_CLAIM_MAX_ATTEMPTS": self.lease_claim_max_attempts,
            "DB_LOCK_WAIT_TIMEOUT": self.db_lock_wait_timeout,
            "DEFAULT_SCENARIO_TIMEOUT": self.default_scenario_timeout,
            "DEFAULT_STEP_TIMEOUT": self.default_step_timeout,
            "EXTERNAL_STIMULUS_CONFIRM_TIMEOUT": self.external_stimulus_confirm_timeout,
            "WHATSAPP_RESPONSE_WAIT_TIMEOUT": self.whatsapp_response_wait_timeout,
            "MAX_CONCURRENT_EXTERNAL_EFFECT_SCENARIOS": (
                self.max_concurrent_external_effect_scenarios
            ),
            "MAX_CONCURRENT_READ_ONLY_SCENARIOS": self.max_concurrent_read_only_scenarios,
            "MAX_RESPONSE_CHAIN_DEPTH": self.max_response_chain_depth,
            "MIN_SYNTHETIC_STIMULUS_INTERVAL": self.min_synthetic_stimulus_interval,
            "MAX_SYNTHETIC_EXTERNAL_RUNS_PER_MINUTE": (
                self.max_synthetic_external_runs_per_minute
            ),
        }
        for key, value in positive_platform_limits.items():
            if value <= 0:
                raise ValueError(f"{key} must be positive")
        if self.component_health_stale_after <= self.health_poll_interval:
            raise ValueError("COMPONENT_HEALTH_STALE_AFTER must exceed HEALTH_POLL_INTERVAL")
        if self.default_step_timeout > self.default_scenario_timeout:
            raise ValueError("DEFAULT_STEP_TIMEOUT must not exceed DEFAULT_SCENARIO_TIMEOUT")
        retry_limits = {
            "OPENAI_TRANSIENT_RETRY_MAX": self.openai_transient_retry_max,
            "READ_ONLY_MAX_RETRIES": self.read_only_max_retries,
            "INTERNAL_IDEMPOTENT_MAX_RETRIES": self.internal_idempotent_max_retries,
        }
        for key, value in retry_limits.items():
            if value < 0:
                raise ValueError(f"{key} must be non-negative")
        if self.external_effect_blind_retries != 0:
            raise ValueError("EXTERNAL_EFFECT_BLIND_RETRIES must be zero")
        if self.time_based_finding_auto_resolution:
            raise ValueError("TIME_BASED_FINDING_AUTO_RESOLUTION must remain false in V1")
        if self.default_external_effect_budget != 0:
            raise ValueError("DEFAULT_EXTERNAL_EFFECT_BUDGET must be zero in V1")
        if not 0 < self.disk_warning < self.disk_critical < self.disk_block_new_heavy_tests <= 100:
            raise ValueError(
                "DISK_WARNING, DISK_CRITICAL and DISK_BLOCK_NEW_HEAVY_TESTS "
                "must be monotonic percentages"
            )
        return self


settings = Settings()
