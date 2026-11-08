"""Launch SUMO's osmWebWizard using a working Overpass mirror.

The wizard's default first server (osm.hpi.de) can accept a request and never
answer. osmGet.py downloads without a timeout, so the wizard then hangs on
"Downloading map data" and never tries another server. This launcher changes
the server for this run only and leaves the SUMO install untouched.

Usage:  python run_osm_wizard.py [any osmWebWizard.py options]
Override the server with the OVERPASS_URL environment variable.
"""
import os
import runpy
import socket
import ssl
import sys
import time
import urllib.error

# Comma-separated: osmGet alternates between these servers while retrying.
MIRROR = os.environ.get(
    "OVERPASS_URL",
    "https://overpass.openstreetmap.fr/api/interpreter,https://overpass-api.de/api/interpreter",
)
TIMEOUT_SECONDS = 300
# Overpass servers often return short-lived 502/504 errors under load. osmGet's
# defaults give up after about 15 seconds of waiting, so wait longer.
RETRIES = "12"
RETRY_DELAY_SECONDS = "10"
CONNECTION_ATTEMPTS = 4
CONNECTION_RETRY_SECONDS = 20

tools = os.path.join(os.environ["SUMO_HOME"], "tools")
sys.path.insert(0, tools)

# Fail with an error instead of hanging forever if the mirror stops responding.
socket.setdefaulttimeout(TIMEOUT_SECONDS)

import osmGet  # noqa: E402

# Avast re-signs HTTPS traffic with its own root, which is in the Windows trust
# store but not in certifi's bundle. Make osmGet use the Windows store even when
# certifi is installed. Certificates are still verified.
osmGet.HAVE_CERTIFI = False

original_create_default_context = ssl.create_default_context


def create_context_without_expired_certs(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    """Build the usual verifying context from the Windows store, minus expired certs.

    The Windows store can hold an expired copy of an intermediate, such as the
    ISRG Root X2 cross-certificate that ended in September 2025. OpenSSL then
    prefers that copy over the valid one the server sends and fails with
    "certificate has expired". Hostname and chain checks are unchanged.
    """
    if any(kwargs.values()):
        return original_create_default_context(purpose, **kwargs)
    store = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    store.load_default_certs(purpose)
    now = time.time()
    valid = [
        der
        for info, der in zip(store.get_ca_certs(), store.get_ca_certs(binary_form=True))
        if ssl.cert_time_to_seconds(info["notAfter"]) > now
    ]
    return original_create_default_context(purpose, cadata=b"".join(valid))


ssl.create_default_context = create_context_without_expired_certs

print("Overpass mirror: %s | Python: %s" % (MIRROR, sys.executable))

original_get = osmGet.get


def get_with_mirror(args=None):
    full_args = list(args or []) + ["-u", MIRROR, "--retries", RETRIES, "--retry-delay", RETRY_DELAY_SECONDS]
    # osmGet drops a server on its first connection or certificate error instead
    # of retrying, so a brief failure can abort the build. Start over a few times
    # first. Certificates are still verified on every attempt.
    for attempt in range(1, CONNECTION_ATTEMPTS + 1):
        try:
            return original_get(full_args)
        except urllib.error.HTTPError:
            raise  # osmGet has already retried these
        except urllib.error.URLError as error:
            if attempt == CONNECTION_ATTEMPTS:
                raise
            print("Download attempt %d of %d failed (%s), trying again in %d seconds."
                  % (attempt, CONNECTION_ATTEMPTS, error.reason, CONNECTION_RETRY_SECONDS))
            time.sleep(CONNECTION_RETRY_SECONDS)


osmGet.get = get_with_mirror

script = os.path.join(tools, "osmWebWizard.py")
sys.argv[0] = script
runpy.run_path(script, run_name="__main__")
