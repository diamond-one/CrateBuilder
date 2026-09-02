from dataclasses import asdict, dataclass, field

ROLES = ("Alternate kick", "Roll / short fill", "Alt snare / rim / clap", "Cymbal",
         "Main kick", "Main snare", "Closed hi-hat", "Open hi-hat")
KINDS = ("kick", "snare", "rim", "clap", "hat", "open_hat", "cymbal", "fill", "perc", "unknown")


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

    @property
    def by_id(self):
        return {s.id: s for s in self.samples}

    def to_dict(self):
        return {"schema": 1, **asdict(self)}

    @classmethod
    def from_dict(cls, data):
        if data.get("schema") != 1:
            raise ValueError("Unsupported session version.")
        if len(data["pads"]) != 64:
            raise ValueError("A session must contain exactly 64 pads.")
        session = cls(root=data["root"], samples=[Sample(**s) for s in data["samples"]],
                      pads=[Assignment(**p) for p in data["pads"]], seed=int(data.get("seed", 1000)),
                      style=data.get("style", "Balanced"), bpm=int(data.get("bpm", 90)),
                      allow_substitutes=bool(data.get("allow_substitutes", True)),
                      scan_notes=data.get("scan_notes", []))
        if any(p.sample_id and p.sample_id not in session.by_id for p in session.pads):
            raise ValueError("The session references a missing sample record.")
        return session


def pad_name(index):
    return f"{'ABCD'[index // 16]}{index % 16 + 1:02d}"
