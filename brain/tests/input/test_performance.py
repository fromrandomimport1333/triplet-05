"""Done-when: at least 200 messages/s with < 5 ms per message."""

import time

from src.common.envelope import seal

from .conftest import TWIN_KEY

N = 2000


def test_throughput(ready):
    payloads = []
    for i in range(N):
        body = ready.reading(value=1.0 + (i % 100) / 100)
        payloads.append(seal(body, TWIN_KEY))
    topic = "city/port/sensor/water_level/water-level-04"

    start = time.perf_counter()
    for p in payloads:
        ready.service.handle(topic, p)
    elapsed = time.perf_counter() - start

    assert len(ready.bus.on("brain/internal/reading")) == N
    per_msg_ms = 1000 * elapsed / N
    rate = N / elapsed
    print(f"\n{rate:.0f} msg/s, {per_msg_ms:.3f} ms per message")
    assert per_msg_ms < 5
    assert rate >= 200
