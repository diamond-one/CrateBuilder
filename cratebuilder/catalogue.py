"""Local, incremental audio catalogue and non-destructive folder labels.

The database contains metadata only. No method opens, renames or writes audio.
Each operation owns its connection so a catalogue can be shared by the UI and
scan worker. Folder rules live separately from disposable analysis checkpoints.
"""
from contextlib import contextmanager
from dataclasses import fields, replace
from datetime import datetime, timezone
import json
import ntpath
import os
from pathlib import Path
import sqlite3

from .model import Sample


SCHEMA_VERSION = 1
RULE_KINDS = frozenset({"perc", "foley", "kick", "snare", "rim", "clap", "hat",
                        "open_hat", "cymbal", "fill", "exclude"})
UNAVAILABLE_STATUSES = frozenset({"offline", "online-only", "unavailable", "unreadable",
                                  "error", "missing", "not-one-shot", "excluded"})


def canonical_path(path):
    """Return a lexical absolute path without probing an unavailable source."""
    value = os.fspath(path)
    if not value or not value.strip():
        raise ValueError("Choose a folder or sample path.")
    windows = os.name == "nt" or bool(ntpath.splitdrive(value)[0])
    module = ntpath if windows else os.path
    value = module.normpath(module.abspath(os.path.expanduser(value)))
    return value, value.casefold() if windows else value


def _path_module(path):
    return ntpath if os.name == "nt" or ntpath.splitdrive(path)[0] else os.path


def _inside(path_key, folder_key):
    module = _path_module(folder_key)
    prefix = folder_key.rstrip("\\/") + module.sep
    return path_key == folder_key or path_key.startswith(prefix)


def _tags(values):
    if isinstance(values, str):
        raise ValueError("Tags must be a list of labels.")
    result = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("Each tag must be text.")
        label = " ".join(value.strip().casefold().split())
        if label and label not in result:
            result.append(label)
    return result


def resolve_folder_rules(path, rules):
    """Resolve a saved rule snapshot without accessing SQLite or audio files.

    Tags accumulate from broad folders to specific folders. The closest rule
    with an explicit kind wins; a tags-only child inherits its parent's kind.
    Thus a child can explicitly restore foley within an excluded parent.
    Nonrecursive rules apply only to files directly within their folder.
    """
    _, path_key = canonical_path(path)
    matched = []
    for rule in rules:
        folder, folder_key = canonical_path(rule["folder"])
        if rule.get("recursive", True):
            applies = _inside(path_key, folder_key)
        else:
            applies = path_key == folder_key or _path_module(path_key).dirname(path_key) == folder_key
        if applies:
            matched.append((len(folder_key), folder_key, folder, rule))
    tags, kind, folder = [], None, None
    for _, _, folder, rule in sorted(matched):
        for tag in _tags(rule.get("tags", [])):
            if tag not in tags:
                tags.append(tag)
        if rule.get("kind") is not None:
            kind = rule["kind"]
    return {"tags": tags, "kind": kind, "folder": folder}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sample_from_json(payload):
    try:
        value = json.loads(payload)
        names = {field.name for field in fields(Sample)}
        return Sample(**{key: item for key, item in value.items() if key in names})
    except (ValueError, TypeError, AttributeError):
        # An incompatible or interrupted old cache is disposable; user rules
        # are in another table and remain usable.
        return None


class Catalogue:
    def __init__(self, db_path):
        if os.fspath(db_path) == ":memory:":
            raise ValueError("The catalogue needs a local database file.")
        self.db_path = Path(db_path).expanduser().absolute()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise ValueError("This catalogue was created by a newer CrateBuilder version.")
            db.execute("""CREATE TABLE IF NOT EXISTS folder_rules (
                path_key TEXT PRIMARY KEY,
                folder TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                kind TEXT,
                recursive INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS sample_cache (
                path_key TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                analysis_version TEXT NOT NULL,
                sample_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS scan_status (
                path_key TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                root_key TEXT,
                root TEXT,
                checked_at TEXT NOT NULL
            )""")
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("PRAGMA synchronous=NORMAL")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _rule_values(folder, tags, kind, recursive):
        if kind is not None and kind not in RULE_KINDS:
            raise ValueError(f"Unknown folder sound type: {kind}")
        folder, key = canonical_path(folder)
        return key, folder, json.dumps(_tags(tags), ensure_ascii=False), kind, int(bool(recursive)), _now()

    def set_folder_rule(self, folder, tags, kind=None, recursive=True):
        values = self._rule_values(folder, tags, kind, recursive)
        with self._connection() as db:
            db.execute("""INSERT INTO folder_rules
                (path_key,folder,tags_json,kind,recursive,updated_at) VALUES (?,?,?,?,?,?)
                ON CONFLICT(path_key) DO UPDATE SET folder=excluded.folder,
                tags_json=excluded.tags_json,kind=excluded.kind,
                recursive=excluded.recursive,updated_at=excluded.updated_at""", values)

    def ensure_folder_rule(self, folder, tags, kind=None, recursive=True):
        """Add a known folder label once, preserving any user's explicit rule."""
        values = self._rule_values(folder, tags, kind, recursive)
        with self._connection() as db:
            cursor = db.execute("""INSERT OR IGNORE INTO folder_rules
                (path_key,folder,tags_json,kind,recursive,updated_at) VALUES (?,?,?,?,?,?)""", values)
            return cursor.rowcount == 1

    def delete_folder_rule(self, folder):
        _, key = canonical_path(folder)
        with self._connection() as db:
            return db.execute("DELETE FROM folder_rules WHERE path_key=?", (key,)).rowcount > 0

    def list_folder_rules(self):
        with self._connection() as db:
            rows = db.execute("SELECT folder,tags_json,kind,recursive FROM folder_rules ORDER BY path_key").fetchall()
        return [{"folder": row["folder"], "tags": json.loads(row["tags_json"]),
                 "kind": row["kind"], "recursive": bool(row["recursive"])} for row in rows]

    def rules_for(self, path, rules=None):
        return resolve_folder_rules(path, self.list_folder_rules() if rules is None else rules)

    def get_sample(self, path, size, mtime_ns, analysis_version):
        """Return a compatible checkpoint, or None when analysis is required."""
        _, key = canonical_path(path)
        with self._connection() as db:
            row = db.execute("""SELECT sample_json FROM sample_cache WHERE path_key=?
                AND size=? AND mtime_ns=? AND analysis_version=?""",
                             (key, int(size), int(mtime_ns), str(analysis_version))).fetchone()
        return _sample_from_json(row[0]) if row else None

    @staticmethod
    def _record_status(db, path, key, status, detail, root):
        root_path, root_key = canonical_path(root) if root is not None else (None, None)
        db.execute("""INSERT INTO scan_status
            (path_key,path,status,detail,root_key,root,checked_at) VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(path_key) DO UPDATE SET path=excluded.path,status=excluded.status,
            detail=excluded.detail,root_key=COALESCE(excluded.root_key,scan_status.root_key),
            root=COALESCE(excluded.root,scan_status.root),checked_at=excluded.checked_at""",
                   (key, path, status, str(detail), root_key, root_path, _now()))

    def save_sample(self, sample, analysis_version, root=None):
        """Commit one analysis and its availability as a recoverable checkpoint."""
        path, key = canonical_path(sample.path)
        # Serialize before opening the transaction so a bad payload cannot leave
        # availability updated without its corresponding analysis.
        payload = json.dumps(sample.to_dict(), ensure_ascii=False, allow_nan=False)
        with self._connection() as db:
            db.execute("""INSERT INTO sample_cache
                (path_key,path,size,mtime_ns,analysis_version,sample_json,updated_at)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(path_key) DO UPDATE SET
                path=excluded.path,size=excluded.size,mtime_ns=excluded.mtime_ns,
                analysis_version=excluded.analysis_version,sample_json=excluded.sample_json,
                updated_at=excluded.updated_at""",
                       (key, path, int(sample.size), int(sample.mtime_ns),
                        str(analysis_version), payload, _now()))
            self._record_status(db, path, key, "available", "", root)

    def record_scan_status(self, path, status, detail="", root=None):
        """Record a failed/excluded file without removing its last good analysis."""
        path, key = canonical_path(path)
        if not isinstance(status, str) or not status.strip():
            raise ValueError("A scan status is required.")
        with self._connection() as db:
            self._record_status(db, path, key, status.strip().casefold(), detail, root)

    def scan_statuses(self, root=None):
        with self._connection() as db:
            rows = db.execute("SELECT * FROM scan_status ORDER BY path_key").fetchall()
        if root is not None:
            _, root_key = canonical_path(root)
            rows = [row for row in rows if _inside(row["path_key"], root_key)]
        return [{"path": row["path"], "status": row["status"], "detail": row["detail"],
                 "root": row["root"], "checked_at": row["checked_at"]} for row in rows]

    def cached_samples(self, root, analysis_version=None):
        """Recover known records even when a network/cloud source is unavailable.

        Missing and failed files are retained but marked unavailable, including
        when their source folder was disconnected after its last good scan.
        This overlay does not change stored analysis or the raw get_sample cache.
        """
        _, root_key = canonical_path(root)
        prefix = root_key.rstrip("\\/") + _path_module(root_key).sep
        query = "SELECT path_key,sample_json FROM sample_cache WHERE (path_key=? OR substr(path_key,1,?)=?)"
        args = [root_key, len(prefix), prefix]
        if analysis_version is not None:
            query += " AND analysis_version=?"
            args.append(str(analysis_version))
        query += " ORDER BY path_key"
        with self._connection() as db:
            rows = db.execute(query, args).fetchall()
            if not rows:
                return []
            # One snapshot also includes source-folder statuses outside a
            # requested subfolder. Lookup by ancestor is O(path depth), avoiding
            # comparing every unavailable file with every cached sample.
            statuses = {row["path_key"]: row["status"] for row in
                        db.execute("SELECT path_key,status FROM scan_status")}
        result = []
        for row in rows:
            sample = _sample_from_json(row["sample_json"])
            if sample is None:
                continue
            key = row["path_key"]
            available = sample.features.get("available", True)
            if statuses.get(key) == "available":
                available = True
            while key:
                if statuses.get(key) in UNAVAILABLE_STATUSES:
                    available = False
                    break
                parent = _path_module(key).dirname(key)
                if parent == key:
                    break
                key = parent
            result.append(replace(sample, features={**sample.features, "available": bool(available)}))
        return result
