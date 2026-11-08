# All-in-one overlay

Used by `Dockerfile.allinone` (one container: broker + every Brain module + mocks + timeline).
The broker runs in the same container, so the broker host stays `127.0.0.1` (config/broker.yaml);
only the HTTP servers listen on every interface and the LLM reaches Ollama on the host machine.
