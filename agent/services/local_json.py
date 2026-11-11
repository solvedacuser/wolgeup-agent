"""Atomic JSON files for the local workspace."""

import json
import os
import tempfile
import time


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique sibling files prevent concurrent writers sharing a .tmp path.
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(5):
            try:
                os.replace(name, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))
