from dataclasses import asdict, dataclass, field

ROLES = ("Alternate kick", "Roll / short fill", "Alt snare / rim / clap", "Cymbal",
         "Main kick", "Main snare", "Closed hi-hat", "Open hi-hat")
KINDS = ("kick", "snare", "rim", "clap", "hat", "open_hat", "cymbal", "fill", "perc", "unknown")
CREATIVE_ROLES = ("Low percussion", "Dry click / knock", "Shake / rattle", "Ringing accent",
                  "Foley hit", "Scrape / texture", "Character accent", "Wildcard")
KINDS = KINDS + ("foley",)


def role_index(index, kit_size=8):
    if kit_size not in (8, 16):
        raise ValueError("Kit size must be 8 or 16.")
    return index % kit_size


def role_names(kit_size=8):
    role_index(0, kit_size)
    return ROLES if kit_size == 8 else ROLES + CREATIVE_ROLES


def kit_count(session_or_size=8):
    size = getattr(session_or_size, "kit_size", session_or_size)
    role_index(0, size)
    return len(session_or_size.pads) // size if hasattr(session_or_size, "pads") else 64 // size


def sp_pad_number(index):
    """Translate a bottom-up logical 4x4 position to the SP's top-down numbers."""
    local = index % 16
    return (3 - local // 4) * 4 + local % 4 + 1


def is_sp_target(target):
    return str(target).lower().replace("-", "").replace("_", "") in ("sp", "sp404", "sp404mk2", "sp404mkii")


def hardware_pad_name(index, target="mpc"):
    if not 0 <= index < 64:
        raise ValueError("Pad index must be 0–63.")
    return f"{'ABCD'[index // 16]}{sp_pad_number(index):02d}" if is_sp_target(target) else pad_name(index)


@dataclass
class Sample:
    id: str
    path: str
    relative: str
    kind: str
    confidence: float
    classification: str
    duration: float
    rate: int
    channels: int
    fingerprint: str
    size: int
    mtime_ns: int
    features: dict
    family: list[str] = field(default_factory=list)
    bpm: float | None = None
    tags: list[str] = field(default_factory=list)
    source: str = ""
    base_kind: str = ""
    base_confidence: float | None = None

    def to_dict(self):
        return asdict(self)


@dataclass
class Assignment:
    sample_id: str | None = None
    reason: str = "Not built yet"
    warning: str = ""
    locked: bool = False


@dataclass
class Session:
    root: str
    samples: list[Sample]
    pads: list[Assignment] = field(default_factory=lambda: [Assignment() for _ in range(64)])
    seed: int = 1000
    style: str = "Balanced"
    bpm: int = 90
    allow_substitutes: bool = True
    scan_notes: list[str] = field(default_factory=list)
    kit_size: int = 8
    target: str = "mpc"
    surprise: float = .35
    sources: list[str] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    library_revision: int = 0
    locked_banks: list[int] = field(default_factory=list)
    generation_sources: list[str] | None = None
    source_mode: str = "mix"
    favourites_only: bool = False
    unused_only: bool = False

    @property
    def by_id(self):
        key = (id(self.samples), len(self.samples), self.library_revision)
        if getattr(self, "_by_id_key", None) != key:
            self._by_id_cache = {s.id: s for s in self.samples}
            self._by_id_key = key
        return self._by_id_cache

    def to_dict(self):
        return {"schema": 2, **asdict(self)}

    @classmethod
    def from_dict(cls, data):
        if data.get("schema") not in (1, 2):
            raise ValueError("Unsupported session version.")
        if len(data["pads"]) != 64:
            raise ValueError("A session must contain exactly 64 pads.")
        locked_banks = data.get("locked_banks", [])
        if (not isinstance(locked_banks, list)
                or any(isinstance(bank, bool) or not isinstance(bank, int) or bank not in range(4)
                       for bank in locked_banks)
                or len(set(locked_banks)) != len(locked_banks)):
            raise ValueError("Locked banks must be unique integer indices from 0 to 3.")
        generation_sources = data.get("generation_sources")
        if (generation_sources is not None and (not isinstance(generation_sources, list)
                or any(not isinstance(source, str) or not source.strip() for source in generation_sources))):
            raise ValueError("Generation sources must be a list of source IDs, or None for all sources.")
        source_mode = data.get("source_mode", "mix")
        if source_mode not in ("mix", "same_pack"):
            raise ValueError("Source mode must be mix or same_pack.")
        if any(not isinstance(data.get(field, False), bool) for field in ("favourites_only", "unused_only")):
            raise ValueError("Generation mark filters must be True or False.")
        session = cls(root=data["root"], samples=[Sample(**s) for s in data["samples"]],
                      pads=[Assignment(**p) for p in data["pads"]], seed=int(data.get("seed", 1000)),
                      style=data.get("style", "Balanced"), bpm=int(data.get("bpm", 90)),
                      allow_substitutes=bool(data.get("allow_substitutes", True)),
                      scan_notes=data.get("scan_notes", []), kit_size=int(data.get("kit_size", 8)),
                      target=data.get("target", "mpc"), surprise=float(data.get("surprise", .35)),
                      sources=list(data.get("sources", [])), history=list(data.get("history", [])),
                      library_revision=int(data.get("library_revision", 0)),
                      locked_banks=list(locked_banks),
                      generation_sources=list(dict.fromkeys(generation_sources)) if generation_sources is not None else None,
                      source_mode=source_mode, favourites_only=data.get("favourites_only", False),
                      unused_only=data.get("unused_only", False))
        role_index(0, session.kit_size)
        if not 0 <= session.surprise <= 1:
            raise ValueError("Surprise must be between 0 and 1.")
        if any(p.sample_id and p.sample_id not in session.by_id for p in session.pads):
            raise ValueError("The session references a missing sample record.")
        return session


def pad_name(index):
    return f"{'ABCD'[index // 16]}{index % 16 + 1:02d}"
