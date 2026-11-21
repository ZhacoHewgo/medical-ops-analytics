from collections import OrderedDict
import json
from threading import Lock

from app.outpatient import validate_aggregate


_snapshots = OrderedDict()
_snapshot_lock = Lock()


def cache_get(content):
    with _snapshot_lock:
        document = _snapshots.get(content)
        if document is not None:
            _snapshots.move_to_end(content)
        return document


def cache_set(content, document):
    with _snapshot_lock:
        _snapshots[content] = document
        _snapshots.move_to_end(content)
        if len(_snapshots) > 4:
            _snapshots.popitem(last=False)


def load_demo_aggregate(path):
    content = path.read_bytes()
    cacheable = len(content) <= 2_000_000
    if cacheable and (document := cache_get(content)) is not None:
        return document
    document = validate_aggregate(json.loads(content.decode("utf-8")))
    if cacheable:
        cache_set(content, document)
    return document
