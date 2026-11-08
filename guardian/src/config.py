"""Guardian configuration — every partner address, port and secret comes from the
environment (or a .env file). Nothing is hard-coded (Phase 1 rule R1)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: str | os.PathLike | None) -> None:
    """Minimal .env loader (KEY=VALUE lines). Real env vars always win."""
    if not path:
        return
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _kv_map(v: str | None) -> dict[str, str]:
    """Parse 'a=x,b=y' into {'a': 'x', 'b': 'y'}."""
    out: dict[str, str] = {}
    for part in (v or "").split(","):
        if "=" in part:
            k, val = part.split("=", 1)
            out[k.strip()] = val.strip()
    return out


@dataclass(frozen=True)
class Settings:
    # --- partners (read from env, never hard-coded) ---
    twin_base_url: str = ""
    brain_base_url: str = ""
    # --- MQTT ---
    mqtt_enabled: bool = True
    mqtt_host: str = ""
    mqtt_port: int = 8883
    mqtt_tls: bool = True
    mqtt_ca: str = ""
    mqtt_cert: str = ""
    mqtt_key: str = ""
    mqtt_client_id: str = "guardian"
    topic_in: str = "twin/events"
    topic_verdicts: str = "guardian/verdicts"
    topic_control: str = "guardian/control"
    topic_timeline: str = "triplet/timeline/guardian"
    # --- storage ---
    database_url: str = ""  # empty => in-memory store (tests / quick demo only)
    # --- auth ---
    jwt_keys: dict[str, str] = field(default_factory=dict)  # issuer(kid) -> HS256 secret
    jwt_audience: str = "guardian"
    jwt_max_ttl_s: int = 120
    twin_event_hmac_key: str = ""
    verdict_signing_key_path: str = ""
    # --- mTLS for the exposed API ---
    tls_ca: str = ""
    tls_cert: str = ""
    tls_key: str = ""
    require_mtls: bool = True
    allowed_client_cns: dict[str, str] = field(default_factory=dict)  # CN -> jwt issuer
    api_host: str = "0.0.0.0"
    api_port: int = 8443
    # --- detection / containment tuning ---
    stuck_window_s: int = 300
    quarantine_threshold: int = 3
    quarantine_count_unauthenticated: bool = False
    replay_cache_size: int = 100_000
    replay_window_s: int = 3600
    rollback_window_s: int = 600
    # --- MQTT account (Brain broker: TLS + username/password, no client certificate) ---
    mqtt_username: str = ""
    mqtt_password_file: str = ""
    mqtt_client_id_pub: str = ""   # empty => <MQTT_CLIENT_ID>-pub-<run id>
    mqtt_client_id_sub: str = ""   # empty => <MQTT_CLIENT_ID>-sub-<run id>
    # --- Brain protocol profile (city-brain integration kit); off => native protocol only ---
    brain_protocol_enabled: bool = False
    run_id: str = ""                         # prefix of every id we emit; empty => random per start
    twin_guardian_key: str = ""              # text of twin-guardian.key (stripped)
    guardian_brain_key: str = ""             # text of guardian-brain.key (stripped)
    hmac_tag: str = "guardian"               # our name in an envelope's "hmacs" map
    topic_twin_meta: str = "twin/meta"
    topic_reading: str = "city/{zone_id}/sensor/{type}/{sensor_id}"
    topic_actuator: str = "twin/actuator/{actuator_id}"
    topic_unit: str = "twin/unit/{unit_id}"
    topic_brain_cmd: str = "brain/cmd/{target_id}"
    topic_brain_alert: str = "brain/alert"
    topic_brain_error: str = "brain/error"
    topic_guardian_control: str = "guardian/control"
    topic_out_verdict: str = "guardian/verdict/{sensor_id}"
    topic_out_quarantine: str = "guardian/quarantine/{device_id}"
    topic_out_status: str = "guardian/status"
    clock_skew_s: float = 10.0
    sensor_limits_file: str = ""
    step_window_s: float = 30.0
    anomaly_z_ok: float = 2.0
    anomaly_span: float = 8.0
    anomaly_min_score: float = 0.0
    frozen_count: int = 0            # Brain profile: identical consecutive readings => frozen (0 = off)
    verdict_refresh_s: float = 0.0   # Brain profile: re-send last verdicts after this silence (0 = off)
    ack_timeout_s: float = 4.0
    ack_grace_s: float = 4.0
    ack_quarantine_threshold: int = 2
    ack_startup_grace_s: float = 10.0
    alert_counts_as_inconsistency: bool = True
    brain_publish_replay_verdicts: bool = False
    seq_reset_on_new_meta: bool = True


def read_key(path: str = "", value: str = "") -> str:
    """HMAC key TEXT (kit 5.3): file content or env value with surrounding whitespace stripped,
    used later as UTF-8 bytes. Never hex-decoded. File wins over value."""
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    return (value or "").strip()


def load_settings(env_file: str | None = None) -> Settings:
    """Build Settings from environment variables (optionally pre-loading a .env file)."""
    _load_dotenv(env_file or os.environ.get("GUARDIAN_ENV_FILE", ".env"))
    e = os.environ.get
    brain = _bool(e("BRAIN_PROTOCOL_ENABLED"), False)
    # guardian/# is the only prefix our broker account may write: the legacy timeline topic
    # (triplet/timeline/guardian) would be dropped by the ACL, so default it inside guardian/#.
    default_timeline = "guardian/timeline" if brain else "triplet/timeline/guardian"
    return Settings(
        twin_base_url=e("TWIN_BASE_URL", ""),
        brain_base_url=e("BRAIN_BASE_URL", ""),
        mqtt_enabled=_bool(e("MQTT_ENABLED"), True),
        mqtt_host=e("MQTT_HOST", ""),
        mqtt_port=int(e("MQTT_PORT", "8883")),
        mqtt_tls=_bool(e("MQTT_TLS"), True),
        mqtt_ca=e("MQTT_CA_FILE", ""),
        mqtt_cert=e("MQTT_CERT_FILE", ""),
        mqtt_key=e("MQTT_KEY_FILE", ""),
        mqtt_client_id=e("MQTT_CLIENT_ID", "guardian"),
        topic_in=e("TOPIC_TWIN_EVENTS", "twin/events"),
        topic_verdicts=e("TOPIC_VERDICTS", "guardian/verdicts"),
        topic_control=e("TOPIC_CONTROL", "guardian/control"),
        topic_timeline=e("TOPIC_TIMELINE", default_timeline),
        database_url=e("DATABASE_URL", ""),
        jwt_keys=_kv_map(e("GUARDIAN_JWT_KEYS")),
        jwt_audience=e("JWT_AUDIENCE", "guardian"),
        jwt_max_ttl_s=int(e("JWT_MAX_TTL_S", "120")),
        twin_event_hmac_key=e("TWIN_EVENT_HMAC_KEY", ""),
        verdict_signing_key_path=e("VERDICT_SIGNING_KEY_FILE", ""),
        tls_ca=e("TLS_CA_FILE", ""),
        tls_cert=e("TLS_CERT_FILE", ""),
        tls_key=e("TLS_KEY_FILE", ""),
        require_mtls=_bool(e("REQUIRE_MTLS"), True),
        allowed_client_cns=_kv_map(e("ALLOWED_CLIENT_CNS")),
        api_host=e("API_HOST", "0.0.0.0"),
        api_port=int(e("API_PORT", "8443")),
        stuck_window_s=int(e("STUCK_WINDOW_S", "300")),
        quarantine_threshold=int(e("QUARANTINE_THRESHOLD", "3")),
        quarantine_count_unauthenticated=_bool(e("QUARANTINE_COUNT_UNAUTHENTICATED"), False),
        replay_cache_size=int(e("REPLAY_CACHE_SIZE", "100000")),
        replay_window_s=int(e("REPLAY_WINDOW_S", "3600")),
        rollback_window_s=int(e("ROLLBACK_WINDOW_S", "600")),
        mqtt_username=e("MQTT_USERNAME", ""),
        mqtt_password_file=e("MQTT_PASSWORD_FILE", ""),
        mqtt_client_id_pub=e("MQTT_CLIENT_ID_PUB", ""),
        mqtt_client_id_sub=e("MQTT_CLIENT_ID_SUB", ""),
        brain_protocol_enabled=brain,
        run_id=e("GUARDIAN_RUN_ID", ""),
        twin_guardian_key=read_key(e("TWIN_GUARDIAN_KEY_FILE", ""), e("TWIN_GUARDIAN_KEY", "")),
        guardian_brain_key=read_key(e("GUARDIAN_BRAIN_KEY_FILE", ""), e("GUARDIAN_BRAIN_KEY", "")),
        hmac_tag=e("HMAC_TAG", "guardian"),
        topic_twin_meta=e("TOPIC_TWIN_META", "twin/meta"),
        topic_reading=e("TOPIC_READING", "city/{zone_id}/sensor/{type}/{sensor_id}"),
        topic_actuator=e("TOPIC_ACTUATOR", "twin/actuator/{actuator_id}"),
        topic_unit=e("TOPIC_UNIT", "twin/unit/{unit_id}"),
        topic_brain_cmd=e("TOPIC_BRAIN_CMD", "brain/cmd/{target_id}"),
        topic_brain_alert=e("TOPIC_BRAIN_ALERT", "brain/alert"),
        topic_brain_error=e("TOPIC_BRAIN_ERROR", "brain/error"),
        topic_guardian_control=e("TOPIC_GUARDIAN_CONTROL", "guardian/control"),
        topic_out_verdict=e("TOPIC_OUT_VERDICT", "guardian/verdict/{sensor_id}"),
        topic_out_quarantine=e("TOPIC_OUT_QUARANTINE", "guardian/quarantine/{device_id}"),
        topic_out_status=e("TOPIC_OUT_STATUS", "guardian/status"),
        clock_skew_s=float(e("CLOCK_SKEW_S", "10")),
        sensor_limits_file=e("SENSOR_LIMITS_FILE", ""),
        step_window_s=float(e("STEP_WINDOW_S", "30")),
        anomaly_z_ok=float(e("ANOMALY_Z_OK", "2.0")),
        anomaly_span=float(e("ANOMALY_SPAN", "8.0")),
        anomaly_min_score=float(e("ANOMALY_MIN_SCORE", "0.0")),
        frozen_count=int(e("FROZEN_COUNT", "8" if brain else "0")),
        verdict_refresh_s=float(e("VERDICT_REFRESH_S", "20" if brain else "0")),
        ack_timeout_s=float(e("ACK_TIMEOUT_S", "4")),
        ack_grace_s=float(e("ACK_GRACE_S", "4")),
        ack_quarantine_threshold=int(e("ACK_QUARANTINE_THRESHOLD", "2")),
        ack_startup_grace_s=float(e("ACK_STARTUP_GRACE_S", "10")),
        alert_counts_as_inconsistency=_bool(e("ALERT_COUNTS_AS_INCONSISTENCY"), True),
        brain_publish_replay_verdicts=_bool(e("BRAIN_PUBLISH_REPLAY_VERDICTS"), False),
        seq_reset_on_new_meta=_bool(e("SEQ_RESET_ON_NEW_META"), True),
    )
