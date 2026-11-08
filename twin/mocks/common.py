"""Small helpers shared by the Guardian and Brain stand-ins."""
import hashlib
import hmac
import json
import os
import threading
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
import yaml


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_iso(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


# Environment variables win over the YAML files (same names as the real layers use; see .env.example).
ENV = {"MQTT_HOST": ("twin", "mqtt", "host"), "MQTT_PORT": ("twin", "mqtt", "port"),
       "MQTT_USERNAME": ("twin", "mqtt", "username"), "MQTT_PASSWORD": ("twin", "mqtt", "password"),
       "TSYP_HMAC_KEY": ("twin", "api", "hmac_key"),
       "TWIN_URL": ("mocks", "twin", "api_url"),
       "GUARDIAN_URL": ("mocks", "brain", "guardian_url"), "GUARDIAN_API_KEY": ("mocks", "guardian", "api_key"),
       "BRAIN_GUARDIAN_KEY": ("mocks", "brain", "guardian_key"),
       "TWIN_API_KEY": ("mocks", "brain", "twin_key"), "TWIN_GUARDIAN_KEY": ("mocks", "guardian", "twin_key"),
       "GUARDIAN_PORT": ("mocks", "guardian", "port"), "BRAIN_PORT": ("mocks", "brain", "port")}


def load_config(twin_config_path, mocks_config_path, environ=None):
    """Twin settings (broker, signing key) plus the mocks' own settings, then environment overrides.
    Nothing is hard-coded in the scripts."""
    with open(twin_config_path, encoding="utf-8-sig") as f:
        twin = yaml.safe_load(f)
    with open(mocks_config_path, encoding="utf-8-sig") as f:
        mocks = yaml.safe_load(f)
    environ = os.environ if environ is None else environ
    for var, (which, section, key) in ENV.items():
        if environ.get(var):
            target = twin if which == "twin" else mocks
            target.setdefault(section, {})[key] = int(environ[var]) if key == "port" else environ[var]
    return twin, mocks


def sign(msg, key):
    body = json.dumps(msg, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


def signature_ok(msg, key):
    rest = {k: v for k, v in msg.items() if k != "sig"}
    return isinstance(msg.get("sig"), str) and hmac.compare_digest(msg["sig"], sign(rest, key))


def make_mqtt(twin_cfg, on_message, topics):
    """Connect to the broker named in the Twin's config and subscribe. Reconnects by itself."""
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        client = mqtt.Client()
    m = twin_cfg["mqtt"]
    if m.get("username"):
        client.username_pw_set(m["username"], m.get("password", ""))

    def on_connect(c, userdata, flags, rc, *rest):
        for t in topics:
            c.subscribe(t)

    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(1, 10)
    client.connect(m["host"], int(m["port"]), 30)
    client.loop_start()
    return client


class JsonLines:
    """Append-only log, one JSON object per line. Safe to read while it is being written."""

    def __init__(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.lock = threading.Lock()

    def append(self, obj):
        with self.lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
