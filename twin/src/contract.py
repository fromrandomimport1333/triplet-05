"""Shared contract helpers for the Twin: timestamps, HMAC signatures, JSON Schema validation, error replies,
and environment-variable overrides for settings.

The signature scheme is the one every layer uses (see twin/INTERFACE.md, section 4):
    sig = HMAC-SHA256(key, json.dumps(msg_without_sig, sort_keys=True, separators=(",", ":")))   as hex
"""
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone

CONTRACT = "1.0"


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def sign(msg, key):
    body = json.dumps({k: v for k, v in msg.items() if k != "sig"}, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


def signature_ok(msg, key):
    return isinstance(msg.get("sig"), str) and hmac.compare_digest(msg["sig"], sign(msg, key))


def error_body(code, detail, errors=None):
    """The contract's error message (docs/schemas/error.schema.json). `detail` stays a plain string, as FastAPI clients expect."""
    body = {"contract_version": CONTRACT, "type": "error", "code": code, "detail": detail, "ts": now_iso()}
    if errors:
        body["errors"] = errors
    return body


class Schemas:
    """Loads docs/schemas/*.schema.json and validates messages against them.

    If the jsonschema package or the folder is missing, validation is skipped with one warning, so the Twin still runs.
    """

    def __init__(self, folder):
        self.validators, self.folder = {}, folder
        try:
            import jsonschema
        except ImportError:
            print("[schema] jsonschema is not installed: incoming messages are NOT validated against docs/schemas")
            return
        if not folder or not os.path.isdir(folder):
            print("[schema] folder %s not found: incoming messages are NOT validated" % folder)
            return
        for name in os.listdir(folder):
            if name.endswith(".schema.json"):
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    schema = json.load(f)
                self.validators[name[:-len(".schema.json")]] = jsonschema.Draft202012Validator(schema)

    def errors(self, name, msg):
        """A list of readable problems ("args: 'x' is not of type 'object'"), empty when the message is valid."""
        v = self.validators.get(name)
        if v is None:
            return []
        out = []
        for e in sorted(v.iter_errors(msg), key=lambda e: list(e.path)):
            where = ".".join(str(p) for p in e.path) or "(message)"
            out.append("%s: %s" % (where, e.message))
        return out


# Environment variables win over the YAML file, so keys never need to be written into a committed file.
ENV_OVERRIDES = {
    "TWIN_MQTT_HOST": ("mqtt", "host"), "MQTT_HOST": ("mqtt", "host"),
    "TWIN_MQTT_PORT": ("mqtt", "port"), "MQTT_PORT": ("mqtt", "port"),
    "MQTT_USERNAME": ("mqtt", "username"), "MQTT_PASSWORD": ("mqtt", "password"),
    "TWIN_API_HOST": ("api", "host"), "TWIN_API_PORT": ("api", "port"),
    "TWIN_API_KEY": ("api", "api_key"),
    "TWIN_GUARDIAN_KEY": ("api", "guardian_key"),
    "TWIN_TEST_KEY": ("api", "test_key"),
    "TSYP_HMAC_KEY": ("api", "hmac_key"), "TWIN_HMAC_KEY": ("api", "hmac_key"),
}


def apply_env(cfg, overrides=ENV_OVERRIDES, environ=None):
    environ = os.environ if environ is None else environ
    for var, (section, key) in overrides.items():
        if environ.get(var):
            value = environ[var]
            if key == "port":
                value = int(value)
            elif key == "enabled":
                value = value.strip().lower() in ("1", "true", "yes", "on")
            cfg.setdefault(section, {})[key] = value
    return cfg


# Partner-broker settings (the City Brain protocol profile, `integration:` in city.yaml). Variable -> path in the YAML.
INTEGRATION_ENV = {
    "INTEGRATION_ENABLED": ("enabled",),
    "PARTNER_MQTT_HOST": ("mqtt", "host"), "PARTNER_MQTT_PORT": ("mqtt", "port"),
    "PARTNER_MQTT_USERNAME": ("mqtt", "username"), "PARTNER_MQTT_PASSWORD_FILE": ("mqtt", "password_file"),
    "PARTNER_MQTT_CA_CERT": ("mqtt", "ca_cert"), "PARTNER_MQTT_TLS": ("mqtt", "tls"),
    "TWIN_BRAIN_KEY_FILE": ("keys", "brain"), "TWIN_GUARDIAN_KEY_FILE": ("keys", "guardian"),
}


def apply_integration_env(cfg, environ=None):
    environ = os.environ if environ is None else environ
    for var, path in INTEGRATION_ENV.items():
        value = environ.get(var)
        if not value:
            continue
        if path[-1] == "port":
            value = int(value)
        elif path[-1] in ("enabled", "tls"):
            value = value.strip().lower() in ("1", "true", "yes", "on")
        node = cfg.setdefault("integration", {})
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = value
    return cfg
