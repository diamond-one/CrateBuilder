"""Explainable, role-relative timbre matching; not a learned taste model."""
import hashlib
from collections import Counter

import numpy as np

from .model import Assignment, ROLES

STYLES = ("Balanced", "Warm & dusty", "Bright & crisp", "Short & tight", "Roomy & loose")
ROLE_POOLS = (("kick",), ("fill",), ("rim", "clap", "snare"), ("cymbal",),
              ("kick",), ("snare",), ("hat",), ("open_hat",))


class Matcher:
    def __init__(self, session):
        self.session = session
        self.samples = session.by_id
        self.vectors = {}
        for kind in {s.kind for s in session.samples}:
            group = [s for s in session.samples if s.kind == kind]
            values = np.array([[s.features[f] for f in ("centroid", "decay", "crest", "flatness")] for s in group])
            for row, sample in enumerate(group):
                # Percentiles within each instrument: a dark kick can match a dark snare.
                # Comparing raw kick and snare spectra would reward the wrong sounds.
                self.vectors[sample.id] = np.array([
                    (np.count_nonzero(values[:, col] < values[row, col]) + .5 * np.count_nonzero(values[:, col] == values[row, col])) / len(group)
                    for col in range(4)])

    def pool(self, index):
        kinds = ROLE_POOLS[index % 8]
        pool = [s for s in self.samples.values() if s.kind in kinds]
        if pool or not self.session.allow_substitutes:
            return pool
        # Every fallback is exposed in the UI and the export manifest.
        fallback = {1: ("snare", "rim"), 2: ("snare", "clap", "rim"),
                    3: ("open_hat",), 5: ("rim", "clap"), 6: ("open_hat",), 7: ("cymbal", "hat")}
        return [s for s in self.samples.values() if s.kind in fallback.get(index % 8, ())]

    def quality(self, sample):
        f = sample.features
        return .15 * min(f["crest"] / 16, 1) - min(f["clipped"] * 8, .7) - min(f["leading"], .4) - (1 - sample.confidence) * .3

    def style_score(self, sample):
        bright, decay, crest, flat = self.vectors[sample.id]
        return {"Balanced": 0, "Warm & dusty": .45 * (1 - bright) + .1 * (1 - crest),
                "Bright & crisp": .45 * bright + .1 * crest,
                "Short & tight": .55 * (1 - decay), "Roomy & loose": .55 * decay}.get(self.session.style, 0)

    def rank(self, index, include_current=False):
        session = self.session
        kit_start = index // 8 * 8
        anchors = [self.samples[session.pads[j].sample_id] for j in (kit_start + 4, kit_start + 5)
                   if j != index and session.pads[j].sample_id in self.samples]
        if index % 8 == 0 and session.pads[kit_start + 4].sample_id in self.samples:
            anchors = [self.samples[session.pads[kit_start + 4].sample_id]]
        target = np.mean([self.vectors[s.id] for s in anchors], axis=0) if anchors else np.array([.5] * 4)
        family = set(w for s in anchors for w in s.family)
        usage = Counter(self.samples[p.sample_id].fingerprint for j, p in enumerate(session.pads)
                        if j != index and p.sample_id in self.samples)
        kit_usage = {self.samples[session.pads[j].sample_id].fingerprint for j in range(kit_start, kit_start + 8)
                     if j != index and session.pads[j].sample_id in self.samples}
        result = []
        for s in self.pool(index):
            if not include_current and session.pads[index].sample_id == s.id:
                continue
            distance = float(np.average(np.abs(self.vectors[s.id] - target), weights=[1.2, 1.1, .7, .5]))
            shared = sorted(family.intersection(s.family))
            score = -.95 * distance + self.style_score(s) + self.quality(s)
            score += .65 * bool(shared)
            score -= 1.1 * usage[s.fingerprint] + 4 * (s.fingerprint in kit_usage)
            if index % 8 == 2:
                score += .13 * (s.kind in {"rim", "clap"})
            if s.kind == "fill":
                score -= max(0, s.features["active_duration"] - 2.2) * .15
                if s.bpm:
                    score -= min(abs(s.bpm - session.bpm) / 70, .5)
            random_bytes = hashlib.sha256(f"{session.seed}:{index}:{s.id}".encode()).digest()
            score += int.from_bytes(random_bytes[:2], "little") / 65535 * .11
            reason = "Shared family: " + ", ".join(shared) if shared else "Matched relative brightness, decay and attack"
            if not anchors:
                reason = "Kick anchor selected for character and variation"
            result.append((score, s, reason))
        return sorted(result, key=lambda item: (-item[0], item[1].relative))

    def build(self, kit=None):
        session = self.session
        indices = list(range(64)) if kit is None else list(range(kit * 8, kit * 8 + 8))
        for i in indices:
            if not session.pads[i].locked:
                session.pads[i] = Assignment()
        # Choose all eight kick anchors first, spreading them across the library.
        for i in sorted(indices, key=lambda n: (0 if n % 8 == 4 else 1 if n % 8 == 5 else 2, n)):
            if session.pads[i].locked:
                continue
            ranked = self.rank(i, include_current=True)
            if ranked:
                _, sample, reason = ranked[0]
                session.pads[i] = Assignment(sample.id, reason)
            else:
                session.pads[i] = Assignment(None, "No suitable sample available")
        refresh_warnings(session)
        return session


def refresh_warnings(session):
    samples = session.by_id
    for i, pad in enumerate(session.pads):
        if pad.sample_id not in samples:
            pad.warning = f"Empty: no {ROLES[i % 8].lower()} available"
            continue
        s = samples[pad.sample_id]
        warnings = []
        if s.kind not in ROLE_POOLS[i % 8]:
            warnings.append(f"Substitution: {s.kind.replace('_', ' ')} in {ROLES[i % 8].lower()} slot")
        if s.confidence < .7:
            warnings.append("Uncertain instrument classification: audition this sound")
        others = [j for j, other in enumerate(session.pads)
                  if j != i and other.sample_id in samples and samples[other.sample_id].fingerprint == s.fingerprint]
        if any(j // 8 == i // 8 for j in others):
            warnings.append("Same audio appears twice in this kit")
        elif others:
            warnings.append("Reused across kits (limited library)")
        if s.kind == "fill" and s.bpm and abs(s.bpm - session.bpm) > 3:
            warnings.append(f"Fill labelled {s.bpm:g} BPM; not time-stretched to {session.bpm}")
        if s.kind == "fill" and s.features["active_duration"] > 2.5:
            warnings.append("Long fill/roll: consider a shorter replacement")
        pad.warning = "; ".join(warnings)


def overview(session):
    refresh_warnings(session)
    samples = session.by_id
    filled = sum(p.sample_id in samples for p in session.pads)
    substitutions = sum(bool(p.sample_id in samples and samples[p.sample_id].kind not in ROLE_POOLS[i % 8])
                        for i, p in enumerate(session.pads))
    unique = len({samples[p.sample_id].fingerprint for p in session.pads if p.sample_id in samples})
    return filled, unique, substitutions
