"""Explainable role-relative matching with a reusable library index."""
import hashlib
import heapq
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from scipy.stats import rankdata

from .model import Assignment, kit_count, role_names

STYLES = ("Balanced", "Warm & dusty", "Bright & crisp", "Short & tight", "Roomy & loose")
CREATIVE_POOL = ("perc", "foley", "unknown")
ROLE_POOLS = (("kick",), ("fill",), ("rim", "clap", "snare"), ("cymbal",),
              ("kick",), ("snare",), ("hat",), ("open_hat",)) + (CREATIVE_POOL,) * 8
ROLE_TAGS = ((),) * 8 + (("low-hit",), ("click", "short"), ("rattle",), ("ringing", "tonal"),
                           ("click", "short"), ("textural",), ("ringing", "bright"), ())
BROWSE_RELATED = {0: ("perc",), 1: ("snare", "rim", "perc"), 2: ("snare", "clap", "rim", "perc"),
                  3: ("open_hat", "hat", "perc"), 4: ("perc",), 5: ("rim", "clap", "perc"),
                  6: ("open_hat", "perc"), 7: ("cymbal", "hat", "perc")}
MATCH_FALLBACK = {1: ("snare", "rim"), 2: ("snare", "clap", "rim"), 3: ("open_hat",),
                  5: ("rim", "clap"), 6: ("open_hat",), 7: ("cymbal", "hat")}
MATCHING_VERSION = 4


def audio_identity(sample):
    return sample.features.get("audio_hash") or sample.fingerprint


def source_identity(sample):
    return sample.source or str(Path(sample.path).parent)


def generation_source(sample):
    """Stable NI library ID, or the single user-library selection group."""
    provider = str(sample.features.get("provider", "")).casefold()
    if sample.features.get("ni_source") or provider in ("ni", "native instruments"):
        return str(sample.features.get("ni_source") or sample.source)
    return "__user__"


def pack_identity(sample):
    return generation_source(sample) if generation_source(sample) != "__user__" else source_identity(sample)


def used_recently(sample):
    value = sample.features.get("last_used")
    if not value:
        return False
    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp >= datetime.now(timezone.utc) - timedelta(days=7)
    except (ValueError, TypeError):
        # An unreadable use date is not evidence that a sound is unused.
        return True


def similarity_identity(sample):
    """A soft diversity hint; never used to delete or relabel a sound."""
    f = sample.features
    return (source_identity(sample), sample.kind, round(f.get("centroid", 0) / 80),
            round(f.get("decay", 0) / .03), round(f.get("crest", 0)),
            round(f.get("flatness", 0) / .01), round(f.get("active_duration", sample.duration) / .02))


class Matcher:
    def __init__(self, session):
        self.session, self.samples = session, session.by_id
        selection = None if session.generation_sources is None else tuple(sorted(set(session.generation_sources)))
        minute = int(datetime.now(timezone.utc).timestamp() // 60) if session.unused_only else None
        recent_identities = set(session.history) if session.unused_only else set()
        key = (id(session.samples), len(session.samples), session.library_revision,
               selection, session.source_mode, session.favourites_only, session.unused_only, minute,
               tuple(session.history) if session.unused_only else None)
        cache = getattr(session, "_matching_cache", None)
        if cache is None or cache[0] != key:
            ordered = [s for s in session.samples if s.kind != "excluded" and s.features.get("available", True)
                       and not s.features.get("preview_only") and not s.features.get("hidden")
                       and (selection is None or generation_source(s) in selection)
                       and (not session.favourites_only or s.features.get("favourite", s.features.get("favorite", False)))
                       and (not session.unused_only or (not used_recently(s) and audio_identity(s) not in recent_identities))]
            groups = defaultdict(list)
            for row, sample in enumerate(ordered):
                groups[sample.kind].append(row)
            vectors = np.zeros((len(ordered), 4), dtype=np.float64)
            for indices in groups.values():
                values = np.array([[ordered[i].features.get(f, 0) for f in
                                   ("centroid", "decay", "crest", "flatness")] for i in indices])
                # Same midrank definition as before, using O(n log n) sorting.
                vectors[indices] = (rankdata(values, method="average", axis=0) - .5) / len(indices)
            cache = (key, ordered, vectors, groups, np.array([self.quality(s) for s in ordered]),
                     {s.id: i for i, s in enumerate(ordered)},
                     Counter(source_identity(s) for s in ordered))
            session._matching_cache = cache
        _, self.ordered, self.matrix, self.groups, self.qualities, self.positions, self.source_sizes = cache
        self.vectors = {s.id: self.matrix[i] for i, s in enumerate(self.ordered)}
        self._active_packs = {}

    def _automatic_pool(self):
        recent = set(self.session.history) if self.session.unused_only else set()
        return [sample for sample in self.ordered if not sample.features.get("manual_only")
                and audio_identity(sample) not in recent]

    def _pack_for_kit(self, start):
        if start in self._active_packs:
            return self._active_packs[start]
        candidates = self._automatic_pool()
        eligible = {sample.id for sample in candidates}
        # The main or alternate kick that the user kept provides the pack anchor.
        for slot in (4, 0):
            assignment = self.session.pads[start + slot]
            if assignment.locked and assignment.sample_id in eligible:
                result = pack_identity(self.samples[assignment.sample_id])
                self._active_packs[start] = result
                return result
        packs = defaultdict(list)
        for sample in candidates:
            packs[pack_identity(sample)].append(sample)
        if not packs:
            self._active_packs[start] = None
            return None
        def coverage(samples):
            kinds = {sample.kind for sample in samples}
            core = sum(bool(kinds.intersection(ROLE_POOLS[role])) for role in range(8))
            total = core + (8 * bool(kinds.intersection(CREATIVE_POOL)) if self.session.kit_size == 16 else 0)
            distinct = len({audio_identity(sample) for sample in samples})
            return core, total, min(distinct, self.session.kit_size)
        scores = {source: coverage(samples) for source, samples in packs.items()}
        best = max(scores.values())
        choices = [source for source, score in scores.items() if score == best]
        result = min(choices, key=lambda source: hashlib.sha256(
            f"pack:{self.session.seed}:{start // self.session.kit_size}:{source}".encode()).digest())
        self._active_packs[start] = result
        return result

    def pool(self, index, browse=False):
        role = index % self.session.kit_size
        if browse:
            return self.ordered
        recent = set(self.session.history) if self.session.unused_only else set()
        pack = self._pack_for_kit(index // self.session.kit_size * self.session.kit_size) if self.session.source_mode == "same_pack" else None
        def permitted(sample):
            return (not sample.features.get("manual_only") and audio_identity(sample) not in recent
                    and (self.session.source_mode != "same_pack" or pack_identity(sample) == pack))
        primary = [self.ordered[i] for kind in ROLE_POOLS[role] for i in self.groups.get(kind, ()) if permitted(self.ordered[i])]
        if primary or not self.session.allow_substitutes:
            return primary
        return [self.ordered[i] for kind in MATCH_FALLBACK.get(role, ()) for i in self.groups.get(kind, ()) if permitted(self.ordered[i])]

    def quality(self, sample):
        f = sample.features
        return (.15 * min(f.get("crest", 0) / 16, 1) - min(f.get("clipped", 0) * 8, .7)
                - min(f.get("leading", 0), .4) - (1 - sample.confidence) * .3)

    def style_score(self, sample):
        bright, decay, crest, _ = self.vectors[sample.id]
        return {"Balanced": 0, "Warm & dusty": .45 * (1 - bright) + .1 * (1 - crest),
                "Bright & crisp": .45 * bright + .1 * crest, "Short & tight": .55 * (1 - decay),
                "Roomy & loose": .55 * decay}.get(self.session.style, 0)

    def rank(self, index, include_current=False, browse=False, limit=None,
             for_generation=False, previous_identity=None):
        session = self.session
        if not 0 <= index < len(session.pads):
            raise ValueError("Pad index is outside this session.")
        if limit is not None and limit <= 0:
            return []
        role, size = index % session.kit_size, session.kit_size
        start = index // size * size
        anchors = [self.samples[session.pads[j].sample_id] for j in (start + 4, start + 5)
                   if j != index and session.pads[j].sample_id in self.vectors]
        if role == 0 and session.pads[start + 4].sample_id in self.vectors:
            anchors = [self.samples[session.pads[start + 4].sample_id]]
        target = np.mean([self.vectors[s.id] for s in anchors], axis=0) if anchors else np.array([.5] * 4)
        family = set(w for s in anchors for w in s.family)
        other = [(j, self.samples[p.sample_id]) for j, p in enumerate(session.pads)
                 if j != index and p.sample_id in self.samples]
        usage = Counter(audio_identity(s) for _, s in other)
        kit_samples = [s for j, s in other if start <= j < start + size]
        kit_usage = {audio_identity(s) for s in kit_samples}
        sources = Counter(source_identity(s) for s in kit_samples)
        similar = Counter(similarity_identity(s) for s in kit_samples)
        recent = Counter(session.history[-256:])
        pool = [s for s in self.pool(index, browse) if include_current or session.pads[index].sample_id != s.id]
        if not pool:
            return []
        positions = [self.positions[s.id] for s in pool]
        vectors = self.matrix[positions]
        scores = -.95 * np.average(np.abs(vectors - target), axis=1, weights=[1.2, 1.1, .7, .5])
        scores += self.qualities[positions]
        if session.style == "Warm & dusty":
            scores += .45 * (1 - vectors[:, 0]) + .1 * (1 - vectors[:, 2])
        elif session.style == "Bright & crisp":
            scores += .45 * vectors[:, 0] + .1 * vectors[:, 2]
        elif session.style == "Short & tight":
            scores += .55 * (1 - vectors[:, 1])
        elif session.style == "Roomy & loose":
            scores += .55 * vectors[:, 1]
        reasons = []
        for n, s in enumerate(pool):
            identity = audio_identity(s)
            shared = sorted(family.intersection(s.family))
            scores[n] += (.15 if role >= 8 else .65) * bool(shared)
            scores[n] -= (.35 if browse else 1.1) * usage[identity]
            scores[n] -= (.8 if browse else 4) * (identity in kit_usage)
            if browse and s.kind not in ROLE_POOLS[role]:
                scores[n] -= .55 if s.kind in BROWSE_RELATED.get(role, CREATIVE_POOL) else 1.85
            if role == 2:
                scores[n] += .13 * (s.kind in {"rim", "clap"})
            if s.kind == "fill":
                scores[n] -= max(0, s.features.get("active_duration", s.duration) - 2.2) * .15
                if s.bpm:
                    scores[n] -= min(abs(s.bpm - session.bpm) / 70, .5)
            if size == 16:
                scores[n] -= .10 * max(0, sources[source_identity(s)] - 1)
                scores[n] -= .16 * similar[similarity_identity(s)]
                scores[n] -= min(.20, .04 * recent[identity])
                if role >= 8:
                    scores[n] += .38 * bool(set(s.tags).intersection(ROLE_TAGS[role]))
                    scores[n] -= .04 * math.log1p(self.source_sizes[source_identity(s)])
            random_bytes = hashlib.sha256(f"{session.seed}:{index}:{identity}".encode()).digest()
            scores[n] += int.from_bytes(random_bytes[:2], "little") / 65535 * .11
            if role >= 8:
                tags = sorted(set(s.tags).intersection(ROLE_TAGS[role]))
                reason = "Creative role: " + (", ".join(tags) if tags else "complementary percussion / foley")
                if s.kind == "unknown":
                    reason += "; unclassified sound — audition by ear"
            elif s.kind not in ROLE_POOLS[role]:
                reason = f"Other kind: {s.kind.replace('_', ' ')} — audition before using"
            elif shared:
                reason = "Shared family: " + ", ".join(shared)
            else:
                reason = "Matched relative brightness, decay and attack"
            if not anchors and s.kind in ROLE_POOLS[role] and role < 8:
                reason = "Kick anchor selected for character and variation"
            reasons.append(reason)
        key = lambda n: (-float(scores[n]), pool[n].relative)
        indices = range(len(pool))
        if for_generation:
            # A copied/renamed file is the same choice, not another lottery ticket.
            # Collapse before top-K so aliases cannot hide genuine alternatives.
            representatives = {}
            for n, sample in enumerate(pool):
                identity = audio_identity(sample)
                old = representatives.get(identity)
                if old is None or key(n) < key(old):
                    representatives[identity] = n
            indices = list(representatives.values())
            unused = [n for n in indices if audio_identity(pool[n]) not in kit_usage]
            if unused:
                indices = unused
            alternatives = [n for n in indices if audio_identity(pool[n]) != previous_identity]
            if alternatives:
                indices = alternatives
            # On a first build, use the whole collection before reusing audio
            # across banks. Rerolls prioritize a different sound in this pad.
            if previous_identity is None:
                fresh = [n for n in indices if not usage[audio_identity(pool[n])]]
                if fresh:
                    indices = fresh
            # Filter before truncating. Otherwise a large source or a heavily
            # used head of the ranking can hide compatible, fresh alternatives.
            threshold = max(float(scores[n]) for n in indices) - self._choice_window(index)
            indices = [n for n in indices if scores[n] >= threshold]
            last_seen = {identity: position for position, identity in enumerate(session.history[-128:])}
            fresh = [n for n in indices if audio_identity(pool[n]) not in last_seen]
            if fresh:
                indices = fresh
            elif last_seen:
                oldest = min(last_seen[audio_identity(pool[n])] for n in indices)
                indices = [n for n in indices if last_seen[audio_identity(pool[n])] == oldest]
            if limit is not None:
                sources = defaultdict(list)
                for n in indices:
                    sources[source_identity(pool[n])].append(n)
                rounds = ((depth, n) for rows in sources.values()
                          for depth, n in enumerate(heapq.nsmallest(limit, rows, key=key)))
                indices = [n for _, n in heapq.nsmallest(limit, rounds, key=lambda row: (row[0], *key(row[1])))]
        indices = sorted(indices, key=key) if limit is None or for_generation else heapq.nsmallest(limit, indices, key=key)
        return [(float(scores[n]), pool[n], reasons[n]) for n in indices]

    def _choice_window(self, index):
        surprise = min(1, max(0, self.session.surprise))
        return (.50 + .65 * surprise) if index % self.session.kit_size >= 8 else (.50 + .35 * surprise)

    def _choose(self, ranked, index):
        """Draw reproducibly from compatible candidates, with recent-use cooldown."""
        if not ranked:
            return None
        surprise = min(1, max(0, self.session.surprise))
        creative = index % self.session.kit_size >= 8
        threshold = ranked[0][0] - self._choice_window(index)
        candidates = [item for item in ranked if item[0] >= threshold]
        recent = self.session.history[-128:]
        last_seen = {identity: position for position, identity in enumerate(recent)}
        fresh = [item for item in candidates if audio_identity(item[1]) not in last_seen]
        if fresh:
            candidates = fresh
        else:
            # Once a small pool is exhausted, cycle its least recently used
            # sounds instead of bouncing between the two highest scorers.
            oldest = min(last_seen.get(audio_identity(item[1]), -1) for item in candidates)
            candidates = [item for item in candidates if last_seen.get(audio_identity(item[1]), -1) == oldest]
        by_source = defaultdict(list)
        for item in candidates:
            by_source[source_identity(item[1])].append(item)
        best = None
        temperature = (.10 + .25 * surprise) if creative else (.14 + .20 * surprise)
        for items in by_source.values():
            for item in items:
                digest = hashlib.sha256(f"choice:{self.session.seed}:{index}:{audio_identity(item[1])}".encode()).digest()
                u = (int.from_bytes(digest[:8], "big") + 1) / (2 ** 64 + 1)
                weight = item[0] / temperature - math.log(len(items))
                score = weight - math.log(-math.log(min(u, 1 - 1e-15)))
                if best is None or score > best[0]:
                    best = (score, item)
        return best[1] if best else ranked[0]

    def build(self, kit=None):
        session, size = self.session, self.session.kit_size
        protected_banks = set(session.locked_banks)
        if kit is not None and not 0 <= kit < kit_count(session):
            raise ValueError("Kit index is outside this session.")
        indices = list(range(len(session.pads))) if kit is None else list(range(kit * size, (kit + 1) * size))
        self._active_packs = {}
        if session.source_mode == "same_pack":
            for start in sorted({index // size * size for index in indices}):
                self._pack_for_kit(start)
        previous = {i: audio_identity(self.samples[session.pads[i].sample_id])
                    for i in indices if session.pads[i].sample_id in self.samples}
        for i in indices:
            if not session.pads[i].locked and i // 16 not in protected_banks:
                session.pads[i] = Assignment()
        for i in sorted(indices, key=lambda n: (0 if n % size == 4 else 1 if n % size == 5 else 2, n)):
            if session.pads[i].locked or i // 16 in protected_banks:
                continue
            ranked = self.rank(i, include_current=True, limit=128,
                               for_generation=True, previous_identity=previous.get(i))
            chosen = self._choose(ranked, i)
            if chosen:
                identity = audio_identity(chosen[1])
                reason = chosen[2]
                if identity == previous.get(i):
                    reason += "; No unused alternative for this pad — kept the same sound"
                else:
                    reason += "; varied from suitable matches"
                session.pads[i] = Assignment(chosen[1].id, reason)
                session.history.append(identity)
                session.history = session.history[-512:]
            else:
                session.pads[i] = Assignment(None, "No suitable sample available")
        refresh_warnings(session)
        return session


def refresh_warnings(session):
    samples, size = session.by_id, session.kit_size
    usage = defaultdict(list)
    for i, pad in enumerate(session.pads):
        if pad.sample_id in samples:
            usage[audio_identity(samples[pad.sample_id])].append(i)
    for i, pad in enumerate(session.pads):
        role = i % size
        if pad.sample_id not in samples:
            pad.warning = f"Empty: no {role_names(size)[role].lower()} available"
            continue
        s, warnings = samples[pad.sample_id], []
        if not s.features.get("available", True):
            warnings.append("Source unavailable locally; download or relink it before use")
        if s.kind == "excluded":
            warnings.append("Source excluded by a folder rule; replace this assigned sound")
        elif s.kind not in ROLE_POOLS[role]:
            warnings.append(f"Substitution: {s.kind.replace('_', ' ')} in {role_names(size)[role].lower()} slot")
        if s.confidence < .7:
            warnings.append("Uncertain instrument classification: audition this sound")
        others = [j for j in usage[audio_identity(s)] if j != i]
        if any(j // size == i // size for j in others):
            warnings.append("Same audio appears twice in this kit")
        elif others:
            warnings.append("Reused across kits (limited library)")
        if s.kind == "fill" and s.bpm and abs(s.bpm - session.bpm) > 3:
            warnings.append(f"Fill labelled {s.bpm:g} BPM; not time-stretched to {session.bpm}")
        if s.kind == "fill" and s.features.get("active_duration", s.duration) > 2.5:
            warnings.append("Long fill/roll: consider a shorter replacement")
        pad.warning = "; ".join(warnings)


def overview(session):
    refresh_warnings(session)
    samples = session.by_id
    filled = sum(p.sample_id in samples for p in session.pads)
    substitutions = sum(bool(p.sample_id in samples and samples[p.sample_id].kind not in ROLE_POOLS[i % session.kit_size])
                        for i, p in enumerate(session.pads))
    unique = len({audio_identity(samples[p.sample_id]) for p in session.pads if p.sample_id in samples})
    return filled, unique, substitutions
