"""JsonListFile: one JSON array per file, kept valid after every write, with size-based rotation."""
import glob
import json
import os
import threading
import time
from datetime import datetime, timezone


class JsonListFile:
    """A file holding one JSON array, kept valid after every write.

    With max_bytes set, a full file is renamed <name>-<UTC time>.json and a new one is started. At most
    keep_files files are kept, counting the active one; the oldest renamed files are deleted.
    """

    def __init__(self, path, max_bytes=None, keep_files=5):
        self.lock = threading.Lock()
        self.path, self.max_bytes, self.keep_files = path, max_bytes, keep_files
        self.next_rotation = max_bytes
        self.count = 0
        self._open()

    def _open(self):
        path = self.path
        self.count = 0
        if os.path.exists(path):
            with open(path, "rb") as f:
                raw = f.read()
            try:
                items = json.loads(raw)
                if not isinstance(items, list) or not raw.endswith(b"\n]"):
                    raise ValueError("not a JSON list file")
                self.count = len(items)
            except ValueError:
                saved = "%s.unreadable-%d" % (path, time.time())
                os.replace(path, saved)
                print("[log] %s could not be continued, kept as %s" % (path, saved))
        if os.path.exists(path):
            self.file = open(path, "r+b")
        else:
            self.file = open(path, "w+b")
            self.file.write(b"[\n]")
            self.file.flush()

    def append(self, item):
        entry = json.dumps(item, separators=(",", ":"), ensure_ascii=False)
        with self.lock:
            # Overwrite the closing bracket so the file stays one valid JSON array.
            self.file.seek(-2 if self.count else -1, os.SEEK_END)
            self.file.write(((",\n" if self.count else "") + entry + "\n]").encode("utf-8"))
            self.file.flush()
            self.count += 1
            if self.max_bytes and self.file.tell() >= self.next_rotation:
                self._rotate()

    def _rotated_files(self):
        """Renamed files, oldest first. The UTC time in the name makes this the time order."""
        stem = os.path.splitext(self.path)[0]
        return sorted(glob.glob(glob.escape(stem) + "-????????-??????-??????.json"))

    def _rotate(self):
        stem = os.path.splitext(self.path)[0]
        self.file.close()
        try:
            os.replace(self.path, "%s-%s.json" % (stem, datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")))
            self.next_rotation = self.max_bytes
        except OSError as e:   # for example, another program has the file open
            print("[log] could not rotate %s (%s); will try again after another 1 MB" % (self.path, e))
            self.next_rotation = os.path.getsize(self.path) + 1000000
        self._open()
        for old in self._rotated_files()[:-(self.keep_files - 1) or None]:
            try:
                os.remove(old)
            except OSError as e:
                print("[log] could not delete %s (%s)" % (old, e))

    def read(self):
        with self.lock:
            self.file.seek(0)
            return json.loads(self.file.read())

    def read_recent(self, limit):
        """Newest entries first, continuing into the renamed files if the active one has too few."""
        with self.lock:
            self.file.seek(0)
            rows = json.loads(self.file.read())[::-1]
            for old in reversed(self._rotated_files()):
                if len(rows) >= limit:
                    break
                with open(old, "rb") as f:
                    rows += json.loads(f.read())[::-1]
        return rows[:max(limit, 0)]


