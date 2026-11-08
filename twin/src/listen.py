"""Print every message on the city MQTT topics: a quick live view of what the Twin publishes.

Usage: python listen.py [host] [topic-filter]      e.g.  python twin/src/listen.py 127.0.0.1 "city/#"
"""
import sys

import paho.mqtt.client as mqtt

host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
flt = sys.argv[2] if len(sys.argv) > 2 else "city/#"

try:
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
except AttributeError:
    c = mqtt.Client()


def on_connect(client, userdata, flags, rc, *rest):
    client.subscribe(flt)
    print("listening on %s at %s" % (flt, host))


c.on_connect = on_connect
c.on_message = lambda client, userdata, m: print(m.topic, m.payload.decode())
c.connect(host, 1883, 30)
c.loop_forever()
