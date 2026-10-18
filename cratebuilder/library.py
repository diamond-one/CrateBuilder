"""Persistent local library browsing, personal marks, saved crates and explicit copies.

The store indexes metadata only. Audio is opened solely by export_collection after
an explicit collection-export request. Connections are local to each operation.
"""
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

from .catalogue import canonical_path

RECENT_DAYS = 7
_NI_SQL = "(lower(i.provider) IN ('native instruments', 'ni') OR i.source LIKE 'ni:%')"
_MARKS = "COALESCE(m.favourite,0) AS favourite,COALESCE(m.hidden,0) AS hidden,COALESCE(m.used_count,0) AS used_count,m.last_used"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ni_item(item):
    return str(item.get("provider", "")).casefold() in ("native instruments", "ni") or str(item.get("source", "")).startswith("ni:")


def _default_mark():
    return {"favourite": False, "hidden": False, "used_count": 0, "last_used": None}


class LibraryStore:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript('''
                CREATE TABLE IF NOT EXISTS metadata_items (
                    path_key TEXT PRIMARY KEY,path TEXT NOT NULL,name TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT 'User',pack TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '__user__',category TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT '',subtype TEXT NOT NULL DEFAULT '',bpm REAL,
                    "key" TEXT NOT NULL DEFAULT '',size INTEGER,mtime_ns INTEGER,
                    item_json TEXT NOT NULL,search_text TEXT NOT NULL,updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS library_marks (
                    path_key TEXT PRIMARY KEY,path TEXT NOT NULL,favourite INTEGER NOT NULL DEFAULT 0,
                    hidden INTEGER NOT NULL DEFAULT 0,used_count INTEGER NOT NULL DEFAULT 0,last_used TEXT
                );
                CREATE TABLE IF NOT EXISTS saved_crates (
                    name TEXT PRIMARY KEY COLLATE NOCASE,settings_json TEXT NOT NULL,updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS library_source_category ON metadata_items(source,category,name COLLATE NOCASE,path_key);
                CREATE INDEX IF NOT EXISTS library_category_name ON metadata_items(category,name COLLATE NOCASE,path_key);
                CREATE INDEX IF NOT EXISTS library_provider_source ON metadata_items(provider,source);
                CREATE INDEX IF NOT EXISTS library_name ON metadata_items(name COLLATE NOCASE,path_key);
                CREATE INDEX IF NOT EXISTS library_mark_filters ON library_marks(hidden,favourite,last_used);
            ''')

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def upsert_items(self, items):
        """Index an iterable in checkpointed batches, preserving all personal marks."""
        query = '''INSERT INTO metadata_items
            (path_key,path,name,provider,pack,source,category,kind,subtype,bpm,"key",size,mtime_ns,item_json,search_text,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path_key) DO UPDATE SET
            path=excluded.path,name=excluded.name,provider=excluded.provider,pack=excluded.pack,
            source=excluded.source,category=excluded.category,kind=excluded.kind,subtype=excluded.subtype,
            bpm=excluded.bpm,"key"=excluded."key",size=excluded.size,mtime_ns=excluded.mtime_ns,
            item_json=excluded.item_json,search_text=excluded.search_text,updated_at=excluded.updated_at'''
        count, batch = 0, []
        for original in items:
            item = dict(original.to_dict() if hasattr(original, "to_dict") else original)
            path, key = canonical_path(item["path"])
            item["path"] = path
            item["name"] = str(item.get("name") or Path(path).name)
            for field in ("provider", "pack", "source", "category", "kind", "subtype", "key"):
                item[field] = str(item.get(field) or ("User" if field == "provider" else "__user__" if field == "source" else ""))
            bpm = item.get("bpm")
            try:
                bpm = float(bpm) if bpm not in (None, "") else None
            except (TypeError, ValueError):
                bpm = None
            item["bpm"] = bpm
            payload = json.dumps(item, ensure_ascii=False, allow_nan=False)
            search = " ".join(str(item.get(field) or "") for field in
                              ("name", "provider", "pack", "source", "category", "kind", "subtype", "bpm", "key", "path")).casefold()
            batch.append((key, path, item["name"], item["provider"], item["pack"], item["source"], item["category"],
                          item["kind"], item["subtype"], bpm, item["key"], item.get("size"), item.get("mtime_ns"), payload, search, _now()))
            if len(batch) == 500:
                with self._connection() as db:
                    db.executemany(query, batch)
                count += len(batch)
                batch.clear()
        if batch:
            with self._connection() as db:
                db.executemany(query, batch)
            count += len(batch)
        return count

    def list_sources(self):
        with self._connection() as db:
            rows = db.execute(f'''SELECT i.source,MAX(i.pack) AS pack,MAX(i.provider) AS provider,
                COUNT(*) AS count,SUM(COALESCE(m.hidden,0)) AS hidden_count FROM metadata_items i
                LEFT JOIN library_marks m ON m.path_key=i.path_key WHERE {_NI_SQL}
                GROUP BY i.source ORDER BY pack COLLATE NOCASE,i.source''').fetchall()
            user = db.execute(f'''SELECT COUNT(*) AS count,SUM(COALESCE(m.hidden,0)) AS hidden_count
                FROM metadata_items i LEFT JOIN library_marks m ON m.path_key=i.path_key WHERE NOT {_NI_SQL}''').fetchone()
        result = []
        if user["count"]:
            result.append({"id": "__user__", "name": "Your samples", "provider": "User", "count": user["count"],
                           "hidden_count": user["hidden_count"] or 0, "root": None})
        result.extend({"id": row["source"], "name": row["pack"] or row["source"], "provider": row["provider"],
                       "count": row["count"], "hidden_count": row["hidden_count"] or 0,
                       "root": row["source"][3:] if row["source"].startswith("ni:") else None} for row in rows)
        return result

    def _where(self, search="", source=None, category=None, favourites_only=False, hidden_only=False, unused_only=False):
        clauses, arguments = ["COALESCE(m.hidden,0)=?"], [int(bool(hidden_only))]
        if favourites_only:
            clauses.append("COALESCE(m.favourite,0)=1")
        if unused_only:
            clauses.append("(m.last_used IS NULL OR datetime(m.last_used)<datetime(?))")
            arguments.append((datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)).isoformat())
        if source is not None:
            if source == "__user__":
                clauses.append("NOT " + _NI_SQL)
            else:
                clauses.append("i.source=?")
                arguments.append(str(source))
        if category:
            clauses.append("i.category=?")
            arguments.append(str(category))
        for term in str(search or "").casefold().split():
            escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("i.search_text LIKE ? ESCAPE '\\'")
            arguments.append("%" + escaped + "%")
        return " AND ".join(clauses), arguments

    def query_items(self, search="", source=None, category=None, favourites_only=False,
                    hidden_only=False, unused_only=False, limit=200, offset=0):
        if isinstance(limit, bool) or isinstance(offset, bool) or not isinstance(limit, int) or not isinstance(offset, int) or limit < 0 or offset < 0:
            raise ValueError("Library limit and offset must be non-negative integers.")
        where, arguments = self._where(search, source, category, favourites_only, hidden_only, unused_only)
        with self._connection() as db:
            rows = db.execute(f'''SELECT i.item_json,i.path_key,{_MARKS} FROM metadata_items i
                LEFT JOIN library_marks m ON m.path_key=i.path_key WHERE {where}
                ORDER BY i.name COLLATE NOCASE,i.path_key LIMIT ? OFFSET ?''', [*arguments, limit, offset]).fetchall()
        result = []
        for row in rows:
            item = json.loads(row["item_json"])
            item.update(path_key=row["path_key"], favourite=bool(row["favourite"]), hidden=bool(row["hidden"]),
                        used_count=row["used_count"], last_used=row["last_used"])
            result.append(item)
        return result

    def count_items(self, search="", source=None, category=None, favourites_only=False, hidden_only=False, unused_only=False):
        where, arguments = self._where(search, source, category, favourites_only, hidden_only, unused_only)
        with self._connection() as db:
            return db.execute(f'''SELECT COUNT(*) FROM metadata_items i LEFT JOIN library_marks m ON m.path_key=i.path_key
                WHERE {where}''', arguments).fetchone()[0]

    def set_mark(self, path, favourite=None, hidden=None):
        for value in (favourite, hidden):
            if value is not None and not isinstance(value, bool):
                raise ValueError("Favourite and hidden marks must be True, False or None.")
        path, key = canonical_path(path)
        if favourite is not None or hidden is not None:
            with self._connection() as db:
                db.execute("INSERT INTO library_marks(path_key,path) VALUES (?,?) ON CONFLICT(path_key) DO NOTHING", (key, path))
                if favourite is not None:
                    db.execute("UPDATE library_marks SET favourite=? WHERE path_key=?", (int(favourite), key))
                if hidden is not None:
                    db.execute("UPDATE library_marks SET hidden=? WHERE path_key=?", (int(hidden), key))
        return self.marks_for_paths([path])[key]

    def record_used(self, paths):
        counts, normalized = Counter(), {}
        for path in paths:
            path, key = canonical_path(path)
            counts[key] += 1
            normalized[key] = path
        timestamp = _now()
        with self._connection() as db:
            db.executemany('''INSERT INTO library_marks(path_key,path,used_count,last_used) VALUES (?,?,?,?)
                ON CONFLICT(path_key) DO UPDATE SET used_count=library_marks.used_count+excluded.used_count,
                last_used=excluded.last_used,path=excluded.path''',
                           [(key, normalized[key], count, timestamp) for key, count in counts.items()])
        return sum(counts.values())

    def marks_for_paths(self, paths):
        keys = {canonical_path(path)[1] for path in paths}
        result = {key: _default_mark() for key in keys}
        ordered = list(keys)
        with self._connection() as db:
            for start in range(0, len(ordered), 800):
                chunk = ordered[start:start + 800]
                rows = db.execute("SELECT path_key,favourite,hidden,used_count,last_used FROM library_marks WHERE path_key IN ("
                                  + ",".join("?" for _ in chunk) + ")", chunk).fetchall()
                for row in rows:
                    result[row["path_key"]] = {"favourite": bool(row["favourite"]), "hidden": bool(row["hidden"]),
                                               "used_count": row["used_count"], "last_used": row["last_used"]}
        return result

    def save_crate(self, name, settings):
        name = str(name).strip()
        if not name:
            raise ValueError("Give this crate a name.")
        if not isinstance(settings, dict):
            raise ValueError("Crate settings must be a dictionary.")
        payload = json.dumps(settings, ensure_ascii=False, allow_nan=False)
        with self._connection() as db:
            db.execute('''INSERT INTO saved_crates(name,settings_json,updated_at) VALUES (?,?,?)
                ON CONFLICT(name) DO UPDATE SET name=excluded.name,settings_json=excluded.settings_json,updated_at=excluded.updated_at''',
                       (name, payload, _now()))
        return name

    def list_crates(self):
        with self._connection() as db:
            rows = db.execute("SELECT name,settings_json,updated_at FROM saved_crates ORDER BY name COLLATE NOCASE").fetchall()
        return [{"name": row["name"], "settings": json.loads(row["settings_json"]), "updated_at": row["updated_at"]} for row in rows]

    def delete_crate(self, name):
        with self._connection() as db:
            return db.execute("DELETE FROM saved_crates WHERE name=?", (str(name).strip(),)).rowcount


def apply_library_marks(session, store):
    """Copy personal marks onto analyzed records, preserving all pad assignments."""
    marks = store.marks_for_paths(sample.path for sample in session.samples)
    session.samples = [replace(sample, features={**sample.features, **marks[canonical_path(sample.path)[1]],
                                                "favorite": marks[canonical_path(sample.path)[1]]["favourite"]})
                       for sample in session.samples]
    session.library_revision += 1
    return session


def _safe_component(value, fallback="Collection"):
    result = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or "")).strip(" .")[:100]
    if not result or result in (".", ".."):
        result = fallback
    if re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", result):
        result = "_" + result
    return result


def _inside(path, directory):
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def _hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cancel_check(cancel):
    if cancel is not None and cancel.is_set():
        raise InterruptedError("Collection export cancelled; incomplete copies are marked and originals are unchanged.")


def _publish_collection(staging, folder):
    if staging.resolve().parent != folder.resolve().parent:
        raise ValueError("Collection staging must stay in the destination directory.")
    try:
        staging.rename(folder)
        return
    except PermissionError:
        folder.mkdir()
        marker = folder / "INCOMPLETE.txt"
        marker.write_text("Collection export in progress. Do not use this folder.\n", encoding="utf-8")
        paths = list(staging.rglob("*"))
        for path in paths:
            if path.is_file() and path.name != "COMPLETE.json":
                target = folder / path.relative_to(staging)
                target.parent.mkdir(parents=True, exist_ok=True)
                path.replace(target)
        (staging / "COMPLETE.json").replace(folder / "COMPLETE.json")
        marker.unlink()
        for path in sorted((path for path in paths if path.is_dir()), key=lambda p: len(p.parts), reverse=True):
            path.rmdir()
        staging.rmdir()


def export_collection(items, destination, name, progress=None, cancel=None):
    """Copy explicit selected items, preserving names under pack/category folders.

    Any changed/unavailable source aborts completion. Failed or cancelled work
    remains in a clearly marked .partial directory; originals are never changed.
    """
    selected, seen = [], set()
    destination = Path(destination).expanduser().resolve()
    for original in items:
        _cancel_check(cancel)
        item = dict(original.to_dict() if hasattr(original, "to_dict") else original)
        source = Path(item["path"]).expanduser().resolve(strict=True)
        _, source_key = canonical_path(source)
        if source_key in seen:
            continue
        if not source.is_file():
            raise ValueError(f"Collection source is not a file: {source}")
        roots = [source.parent]
        for field in ("samples_root", "source_root", "root"):
            if item.get(field):
                roots.append(Path(item[field]).expanduser().resolve())
        if str(item.get("source", "")).startswith("ni:"):
            roots.append(Path(item["source"][3:]).expanduser().resolve())
        if any(_inside(destination, root) for root in roots):
            raise ValueError("Choose an export destination outside the source sample folders.")
        stat = source.stat()
        if getattr(stat, "st_file_attributes", 0) & (0x1000 | 0x400000):
            raise ValueError(f"{source.name} is online-only. Make it available offline before exporting.")
        for field, actual in (("size", stat.st_size), ("mtime_ns", stat.st_mtime_ns)):
            expected = item.get(field)
            if expected is not None and int(expected) != actual:
                raise ValueError(f"{source.name} changed since indexing. Update the library before exporting.")
        selected.append((item, source, stat))
        seen.add(source_key)
    if not selected:
        raise ValueError("Choose at least one library item to export.")
    destination.mkdir(parents=True, exist_ok=True)
    base = _safe_component(name)
    folder, number = destination / base, 1
    while folder.exists():
        folder = destination / f"{base}_{number:02d}"
        number += 1
    staging = destination / f".{folder.name}-{uuid.uuid4().hex[:10]}.partial"
    staging.mkdir()
    incomplete = staging / "INCOMPLETE.txt"
    incomplete.write_text("Incomplete collection export. Do not use this folder.\n", encoding="utf-8")
    rows, used = [], set()
    try:
        for index, (item, source, before) in enumerate(selected):
            _cancel_check(cancel)
            if progress:
                progress(f"Copying {index + 1}/{len(selected)}: {source.name}", index / len(selected))
            pack = _safe_component(item.get("pack") or ("Your samples" if not _ni_item(item) else item.get("source")))
            category = _safe_component(item.get("category"), "Unsorted")
            relative = Path(pack) / category / source.name
            if str(relative).casefold() in used:
                suffix = hashlib.sha256(str(source).encode()).hexdigest()[:10]
                relative = Path(pack) / category / ("variant_" + suffix) / source.name
            used.add(str(relative).casefold())
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with source.open("rb") as stream, target.open("xb") as output:
                while chunk := stream.read(1024 * 1024):
                    _cancel_check(cancel)
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            after = source.stat()
            if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                raise ValueError(f"{source.name} changed during copying. This collection is incomplete.")
            fingerprint = digest.hexdigest()
            expected_hash = item.get("fingerprint") or item.get("sha256")
            if expected_hash and expected_hash != fingerprint:
                raise ValueError(f"{source.name} changed since analysis. This collection is incomplete.")
            if target.stat().st_size != before.st_size or _hash_file(target) != fingerprint:
                raise ValueError(f"Copy validation failed: {source.name}")
            rows.append({"file": str(relative), "source_path": str(source), "provider": item.get("provider", "User"),
                         "pack": item.get("pack", ""), "source": item.get("source", ""),
                         "category": item.get("category", ""), "kind": item.get("kind", ""),
                         "subtype": item.get("subtype", ""), "bpm": item.get("bpm"), "key": item.get("key", ""),
                         "size": before.st_size, "mtime_ns": before.st_mtime_ns, "sha256": fingerprint})
        manifest = {"schema": 1, "complete": True, "created_at": _now(), "name": str(name), "files": rows,
                    "note": "Explicit byte-for-byte copies; original files and filenames are preserved. No audio conversion was applied."}
        (staging / "COLLECTION_MANIFEST.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        with (staging / "FILE_MAP.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        _cancel_check(cancel)
        (staging / "COMPLETE.json").write_text(json.dumps({"complete": True, "files": len(rows), "validated": True}), encoding="utf-8")
        incomplete.unlink()
        _publish_collection(staging, folder)
        if progress:
            progress("Collection copied and verified. Originals are unchanged.", 1)
        return folder
    except Exception as exc:
        active = staging if staging.exists() else folder
        if active.exists():
            (active / "INCOMPLETE.txt").write_text(f"Incomplete collection export. Do not use this folder.\n{exc}\n", encoding="utf-8")
            (active / "EXPORT_REPORT.json").write_text(json.dumps({"complete": False, "error": str(exc), "copied_files": rows}, indent=2), encoding="utf-8")
        raise
