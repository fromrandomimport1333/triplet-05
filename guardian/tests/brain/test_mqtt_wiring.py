"""Live MQTT wiring with a fake paho client (paho and a broker are not needed): Last Will set
before connect, status online right after connect, subscriptions, retain flag passed through."""
import asyncio
import json

from src.adapter.envelope import open_envelope
from src.config import Settings


class FakeClient:
    def __init__(self):
        self.calls, self.will, self.published, self.subscribed = [], None, [], []
        self.on_connect = self.on_message = None

    def will_set(self, topic, payload, qos, retain):
        self.calls.append("will_set")
        self.will = (topic, payload, qos, retain)

    def connect(self, host, port):
        self.calls.append("connect")

    def loop_start(self):
        self.calls.append("loop_start")
        if self.on_connect:
            class RC:
                is_failure = False
            self.on_connect(self, None, None, RC())

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))

    def subscribe(self, topics, qos=0):
        self.subscribed.append(topics)


def test_publisher_will_then_online(monkeypatch):
    import src.mqtt.publisher as pubmod
    fake = FakeClient()
    monkeypatch.setattr(pubmod, "make_mqtt_client", lambda s, cid: fake)
    from src.db.session import MemoryStore
    from src.main import build_state
    s = Settings(mqtt_host="broker", brain_protocol_enabled=True, guardian_brain_key="kgb",
                 twin_guardian_key="ktg", topic_timeline="guardian/timeline", run_id="w1")
    st = build_state(s, store=MemoryStore())
    assert fake.calls[:2] == ["will_set", "connect"]
    topic, payload, qos, retain = fake.will
    assert topic == "guardian/status" and qos == 1 and retain is True
    assert open_envelope(payload, "kgb").body["status"] == "offline"
    online = [p for p in fake.published if p[0] == "guardian/status"]
    assert online and open_envelope(online[-1][1], "kgb").body["status"] == "online" and online[-1][3]
    assert st.adapter is not None


def test_brain_subscriber_routes_with_retain_flag():
    from mocks.brain_local_stack import BrainLocalStack
    from src.mqtt.subscriber import BrainSubscriber
    s = BrainLocalStack()
    fake = FakeClient()
    seen = []

    class Spy:
        def subscriptions(self):
            return s.adapter.subscriptions()

        async def handle(self, topic, payload, retained=False):
            seen.append((topic, payload, retained))
            return "ok"

    loop = asyncio.new_event_loop()
    import threading
    th = threading.Thread(target=loop.run_forever, daemon=True)
    th.start()
    sub = BrainSubscriber(s.settings, Spy(), loop, client=fake)
    sub.start()
    assert set(fake.subscribed[0]) == {(t, 1) for t in s.adapter.subscriptions()}
    assert ("city/+/sensor/+/+", 1) in fake.subscribed[0]

    class Msg:
        topic, payload, retain = "twin/meta", b'{"v":1}', 1
    fake.on_message(fake, None, Msg())
    import time
    for _ in range(50):
        if seen:
            break
        time.sleep(0.02)
    loop.call_soon_threadsafe(loop.stop)
    assert seen == [("twin/meta", b'{"v":1}', True)]
    s.close()
