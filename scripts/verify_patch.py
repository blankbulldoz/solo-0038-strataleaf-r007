"""Rerunnable regression verification for Database.patch / merge_patch.

Each scenario derives its expected document from the original document and the
patch alone (an independent reference merge), never from the implementation or
from query/index output.  Assertions cover the reopened document body, the
document revision, expiry facts, the complete change feed, historical reads and
raw SQLite rows so a single wrong document cannot be "confirmed" by a query
that shares the same index projection.

Run with:  python3 scripts/verify_patch.py
Exit status is non-zero if any expectation fails; all scenarios still run.
"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strataleaf import Database, Conflict, InvalidDocument


def reference_merge(document, patch):
    """Minimal independent JSON-Merge-Patch reference used only as an oracle.

    This deliberately uses a different structure from strataleaf.patch: it
    rebuilds containers with recursion and never joins keys into path strings.
    """
    if not isinstance(patch, dict):
        return json.loads(json.dumps(patch))
    result = json.loads(json.dumps(document)) if isinstance(document, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            merged = reference_merge(result.get(key), value)
            result[key] = merged
    return result


class _Missing:
    def __repr__(self):
        return "<missing>"


MISSING = _Missing()


def at(document, *keys):
    """Walk a document without raising, so a missing member is reported."""
    value = document
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return MISSING
        value = value[key]
    return value


class Checker:
    def __init__(self):
        self.passed = 0
        self.failures = []

    def check(self, label, actual, expected):
        if actual != expected:
            self.failures.append(f"{label}: expected {expected!r}, got {actual!r}")
        else:
            self.passed += 1

    def rejects(self, label, call, error=Conflict):
        try:
            call()
        except error:
            self.passed += 1
        except Exception as exc:  # noqa: BLE001 - report the wrong exception
            self.failures.append(f"{label}: expected {error.__name__}, got {type(exc).__name__}: {exc}")
        else:
            self.failures.append(f"{label}: expected {error.__name__}, call succeeded")


def raw_snapshot(path):
    """Read physical facts straight from SQLite, bypassing the library."""
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        versions = [dict(row) for row in connection.execute(
            "SELECT collection,key,rev,body,expires FROM versions ORDER BY rev,collection,key")]
        indexes = [dict(row) for row in connection.execute(
            "SELECT collection,name,token,key FROM index_values ORDER BY collection,name,token,key")]
        commits = [json.loads(row["payload"]) for row in connection.execute(
            "SELECT payload FROM commits ORDER BY rev")]
        head = int(connection.execute("SELECT value FROM meta WHERE key='head'").fetchone()[0])
        floor = int(connection.execute("SELECT value FROM meta WHERE key='floor'").fetchone()[0])
        receipts = [dict(row) for row in connection.execute(
            "SELECT request_key,rev,count FROM receipts ORDER BY request_key")]
    finally:
        connection.close()
    return {"versions": versions, "indexes": indexes, "commits": commits,
            "head": head, "floor": floor, "receipts": receipts}


def scenario_structure(temp):
    """Strict records: empty objects, scalar<->object conversion, null deletes.

    Expected documents are written out as literals from the prompt's stated
    merge meaning; the reference oracle is kept only as a secondary cross-check.
    """
    path = Path(temp) / "structure.db"
    original = {"shape": 7, "items": [{"v": 1}], "keep": {"x": 1}}
    patch = {"shape": {"gone": None}, "fresh": {}, "items": [{"v": 2}]}
    expected = {"shape": {}, "fresh": {}, "items": [{"v": 2}], "keep": {"x": 1}}
    assert reference_merge(original, patch) == expected

    checker = Checker()
    with Database(path) as db:
        db.catalog.create("docs")
        seeded = db.batch().put("docs", "a", original, expires_at=2000000000).commit()
        checker.check("seed revision", seeded.revision, 1)

        result = db.patch("docs", "a", patch, expected=seeded.revision)
        checker.check("patch revision", result.revision, 2)
        checker.check("patch count", result.count, 1)

        after = db.get("docs", "a")
        checker.check("structure document", after.document, expected)
        checker.check("structure expiry", after.expires_at, 2000000000.0)
        checker.check("structure document revision", after.revision, 2)
        # Explicit independent expectations, spelled out rather than only via oracle.
        checker.check("shape is empty object", at(after.document, "shape"), {})
        checker.check("fresh exists as empty object", at(after.document, "fresh"), {})
        checker.check("items replaced wholesale", at(after.document, "items"), [{"v": 2}])
        checker.check("keep untouched", at(after.document, "keep"), {"x": 1})

        historical = db.get("docs", "a", seeded.revision)
        checker.check("structure old revision body", historical.document, original)
        checker.check("structure old revision expiry", historical.expires_at, 2000000000.0)

        feed = db.feed.read(0)
        checker.check("structure feed length", len(feed), 2)
        checker.check("structure feed patch operation", feed[-1]["operations"][0]["document"], expected)
        checker.check("structure feed patch expiry", feed[-1]["operations"][0]["expires_at"], 2000000000)
        checker.check("structure feed patch expected", feed[-1]["operations"][0]["expected"], 1)
        checker.check("structure feed head", feed[-1]["revision"], 2)

        # A second patch exercising the remaining merge boundaries on the same doc.
        second_patch = {
            "keep": {},                      # empty object keeps members
            "newobj": {"only": None},        # new member whose only child is deleted -> {}
            "shape": {"now": 1},             # empty object becomes populated object
            "items": {"not": "a list"},      # array replaced by object
            "scalar_to_null": None,          # delete an absent member
        }
        expected_second = reference_merge(expected, second_patch)
        second = db.patch("docs", "a", second_patch, expected=2)
        checker.check("second patch revision", second.revision, 3)
        current = db.get("docs", "a")
        checker.check("second document", current.document, expected_second)
        checker.check("keep preserved through empty patch", at(current.document, "keep"), {"x": 1})
        checker.check("newobj is empty object", at(current.document, "newobj"), {})
        checker.check("shape populated after empty", at(current.document, "shape"), {"now": 1})
        checker.check("items object replacement", at(current.document, "items"), {"not": "a list"})
        checker.check("scalar_to_null absent", "scalar_to_null" in current.document, False)

        # Deleting a nested member leaves the parent (empty) object in place.
        third = db.patch("docs", "a", {"keep": {"x": None}})
        checker.check("third patch revision", third.revision, 4)
        after_third = db.get("docs", "a")
        checker.check("nested delete keeps parent", at(after_third.document, "keep"), {})
        checker.check("nested delete old revision", db.get("docs", "a", 3).document, expected_second)

        # Object -> scalar conversion (not a delete) and concise scalar -> object.
        fourth_patch = {"newobj": 9, "shape": {"deep": {"leaf": 1}}, "fresh": None}
        expected_fourth = reference_merge(after_third.document, fourth_patch)
        fourth = db.patch("docs", "a", fourth_patch)
        checker.check("fourth patch revision", fourth.revision, 5)
        checker.check("object to scalar conversion", db.get("docs", "a").document, expected_fourth)
        checker.check("object became scalar", at(db.get("docs", "a").document, "newobj"), 9)
        checker.check("empty object deleted by null", "fresh" in db.get("docs", "a").document, False)

        feed = db.feed.read(0)
        checker.check("structure full feed revisions", [c["revision"] for c in feed], [1, 2, 3, 4, 5])
        checker.check("structure feed bodies", [c["operations"][0]["document"] for c in feed],
                      [original, expected, expected_second,
                       reference_merge(expected_second, {"keep": {"x": None}}), expected_fourth])
        checker.check("structure feed expiry facts", [c["operations"][0]["expires_at"] for c in feed],
                      [2000000000, 2000000000, 2000000000, 2000000000, 2000000000])
        checker.check("structure feed expected chain",
                      [c["operations"][0]["expected"] for c in feed], [None, 1, 2, 3, 4])

    # Reopen the same file through the public API and re-check durable facts.
    with Database(path) as reopened:
        final = reopened.get("docs", "a")
        checker.check("structure reopened body", final.document, expected_fourth)
        checker.check("structure reopened revision", final.revision, 5)
        checker.check("structure reopened expiry", final.expires_at, 2000000000.0)
        checker.check("structure reopened head", reopened.revision, 5)
        checker.check("structure reopened feed", [c["revision"] for c in reopened.feed.read(0)], [1, 2, 3, 4, 5])
        checker.check("structure reopened historical", reopened.get("docs", "a", 1).document, original)
        checker.check("structure reopened historical expiry", reopened.get("docs", "a", 2).expires_at, 2000000000.0)

    physical = raw_snapshot(path)
    checker.check("raw structure head", physical["head"], 5)
    checker.check("raw structure version count", len(physical["versions"]), 5)
    checker.check("raw structure latest body", json.loads(physical["versions"][-1]["body"]), expected_fourth)
    checker.check("raw structure latest expiry", physical["versions"][-1]["expires"], 2000000000.0)
    checker.check("raw structure commit revisions", [c["revision"] for c in physical["commits"]], [1, 2, 3, 4, 5])
    return checker


def scenario_literal_dot(temp):
    """Literal member names containing dots are not nested paths."""
    path = Path(temp) / "literal.db"
    checker = Checker()
    original_a = {"prefs": {"code": "A", "keep": 1}, "prefs.code": "literal"}
    original_b = {"prefs": {"code": "B"}}
    patch = {"prefs.code": "B", "prefs": {"keep": 2}}
    expected = {"prefs": {"code": "A", "keep": 2}, "prefs.code": "B"}
    assert reference_merge(original_a, patch) == expected

    with Database(path) as db:
        db.catalog.create("docs")
        seeded_a = db.batch().put("docs", "a", original_a).commit()
        seeded_b = db.batch().put("docs", "b", original_b).commit()
        db.indexes.create("docs", "uq_code", "prefs.code", unique=True)

        result = db.patch("docs", "a", patch, expected=seeded_a.revision)
        checker.check("literal patch revision", result.revision, 3)

        after_a = db.get("docs", "a")
        checker.check("literal document", after_a.document, expected)
        checker.check("literal member set", at(after_a.document, "prefs.code"), "B")
        checker.check("nested code preserved", at(after_a.document, "prefs", "code"), "A")
        checker.check("nested keep updated", at(after_a.document, "prefs", "keep"), 2)
        checker.check("b unchanged", db.get("docs", "b").document, original_b)
        checker.check("b revision unchanged", db.get("docs", "b").revision, seeded_b.revision)
        checker.check("literal old revision", db.get("docs", "a", seeded_a.revision).document, original_a)

        # Deleting the literal member must not touch the nested code.
        deleted = db.patch("docs", "a", {"prefs.code": None})
        checker.check("literal delete revision", deleted.revision, 4)
        after_delete = db.get("docs", "a")
        checker.check("literal member removed", "prefs.code" in after_delete.document, False)
        checker.check("nested code survives literal delete", at(after_delete.document, "prefs"),
                      {"code": "A", "keep": 2})
        checker.check("literal delete old revision", db.get("docs", "a", 3).document, expected)

        # A real change to the nested unique field must still be rejected.
        checker.rejects("nested unique change rejected",
                        lambda: db.patch("docs", "a", {"prefs": {"code": "B"}}), Conflict)
        checker.check("nested rejection leaves body", db.get("docs", "a").document,
                      {"prefs": {"code": "A", "keep": 2}})
        checker.check("nested rejection leaves revision", db.get("docs", "a").revision, 4)
        checker.check("nested rejection leaves head", db.revision, 4)

        # Stale expected must be rejected too, without advancing anything.
        checker.rejects("stale expected rejected",
                        lambda: db.patch("docs", "a", {"prefs": {"keep": 3}}, expected=1), Conflict)
        checker.check("stale leaves revision", db.get("docs", "a").revision, 4)
        checker.check("stale leaves head", db.revision, 4)

        feed = db.feed.read(0)
        checker.check("literal full feed revisions", [c["revision"] for c in feed], [1, 2, 3, 4])
        checker.check("literal feed bodies", [c["operations"][0]["document"] for c in feed],
                      [original_a, original_b, expected, {"prefs": {"code": "A", "keep": 2}}])
        checker.check("literal feed expected chain",
                      [c["operations"][0]["expected"] for c in feed], [None, None, 1, 3])
        inspection = db.diagnostics.inspect(deep=True)
        checker.check("literal deep inspection ok", inspection["ok"], True)
        checker.check("literal deep inspection issues", inspection["issues"], [])

    with Database(path) as reopened:
        checker.check("literal reopened a", reopened.get("docs", "a").document,
                      {"prefs": {"code": "A", "keep": 2}})
        checker.check("literal reopened b", reopened.get("docs", "b").document, original_b)
        checker.check("literal reopened a revision", reopened.get("docs", "a").revision, 4)
        checker.check("literal reopened head", reopened.revision, 4)
        checker.check("literal reopened historical", reopened.get("docs", "a", 3).document, expected)
        checker.check("literal reopened feed", [c["revision"] for c in reopened.feed.read(0)], [1, 2, 3, 4])

    physical = raw_snapshot(path)
    checker.check("literal raw head", physical["head"], 4)
    checker.check("literal raw version count", len(physical["versions"]), 4)
    checker.check("literal raw latest body", json.loads(physical["versions"][-1]["body"]),
                  {"prefs": {"code": "A", "keep": 2}})
    checker.check("literal raw index tokens", physical["indexes"],
                  [{"collection": "docs", "name": "uq_code", "token": 'v:"A"', "key": "a"},
                   {"collection": "docs", "name": "uq_code", "token": 'v:"B"', "key": "b"}])
    checker.check("literal raw commit revisions", [c["revision"] for c in physical["commits"]], [1, 2, 3, 4])
    # Failed writes must not leave versions/commits/receipts behind.
    checker.check("literal no extra versions after failures", len(physical["versions"]), 4)
    checker.check("literal no receipts", physical["receipts"], [])
    checker.check("literal index unchanged by failures", physical["indexes"],
                  [{"collection": "docs", "name": "uq_code", "token": 'v:"A"', "key": "a"},
                   {"collection": "docs", "name": "uq_code", "token": 'v:"B"', "key": "b"}])
    return checker


def scenario_schema_required(temp):
    """A merge that would delete a required nested member must be rejected."""
    path = Path(temp) / "schema.db"
    checker = Checker()
    schema = {"fields": {"prefs.code": "string"}, "required": ["prefs.code"]}
    with Database(path) as db:
        db.catalog.create("docs", schema)
        seeded = db.batch().put("docs", "a", {"prefs": {"code": "A"}}).commit()
        # Deleting the nested required member is a delete of a member, not an
        # empty object: the resulting document omits it and must be rejected.
        checker.rejects("required member delete rejected",
                        lambda: db.patch("docs", "a", {"prefs": {"code": None}}), InvalidDocument)
        checker.check("required delete leaves revision", db.get("docs", "a").revision, seeded.revision)
        checker.check("required delete leaves head", db.revision, seeded.revision)
        checker.check("required delete leaves body", db.get("docs", "a").document, {"prefs": {"code": "A"}})

    physical = raw_snapshot(path)
    checker.check("required raw head", physical["head"], 1)
    checker.check("required raw versions", len(physical["versions"]), 1)
    checker.check("required raw commits", len(physical["commits"]), 1)
    return checker


def main():
    total = Checker()
    with tempfile.TemporaryDirectory(prefix="strataleaf-verify-patch-") as temp:
        for scenario in (scenario_structure, scenario_literal_dot, scenario_schema_required):
            try:
                result = scenario(temp)
            except Exception as exc:  # noqa: BLE001 - one broken scenario must not hide the rest
                total.failures.append(f"{scenario.__name__} raised {type(exc).__name__}: {exc}")
                continue
            total.passed += result.passed
            total.failures.extend(result.failures)
    print(json.dumps({"checks": total.passed, "failures": total.failures}, ensure_ascii=False, indent=2))
    return 0 if not total.failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
