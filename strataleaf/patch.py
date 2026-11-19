from .codec import dumps, loads
from .errors import Conflict, InvalidDocument, NotFound
from .mvcc import read_record
from .transaction import WriteBatch


def merge_patch(document, patch):
    if not isinstance(patch, dict):
        return loads(dumps(patch))
    result = loads(dumps(document)) if isinstance(document, dict) else {}
    updates = []

    def collect(changes, prefix=()):
        for key, value in changes.items():
            path = prefix + (key,)
            if isinstance(value, dict):
                collect(value, path)
            else:
                updates.append((".".join(path), value))

    collect(patch)
    for path, value in updates:
        parts = path.split(".")
        target = result
        for part in parts[:-1]:
            child = target.get(part)
            if not isinstance(child, dict):
                if value is None:
                    target = None
                    break
                child = {}
                target[part] = child
            target = child
        if target is None:
            continue
        if value is None:
            target.pop(parts[-1], None)
        else:
            target[parts[-1]] = loads(dumps(value))
    return result


def patch_document(store, collection, key, patch, expected=None):
    if not isinstance(patch, dict):
        raise InvalidDocument("document merge patch must be an object")
    current = read_record(store, collection, key)
    if current is None:
        raise NotFound("cannot patch a missing document")
    if expected is not None and expected != current.revision:
        raise Conflict("patch revision changed")
    document = merge_patch(current.document, patch)
    return WriteBatch(store).put(collection, key, document, expected=current.revision,
                                             expires_at=current.expires_at).commit()
