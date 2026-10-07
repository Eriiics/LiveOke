"""Utilidades de teoría musical: nombres de notas, escalas y tonalidades."""
from __future__ import annotations

from dataclasses import dataclass

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
NOTE_NAMES_ES = ["Do", "Do#", "Re", "Re#", "Mi", "Fa", "Fa#", "Sol", "Sol#", "La", "La#", "Si"]
FLAT_ALIASES = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}

# Intervalos (semitonos desde la tónica) de cada escala
SCALES = {
    "major": [0, 2, 4, 5, 7, 9, 11],
    "minor": [0, 2, 3, 5, 7, 8, 10],
    "chromatic": list(range(12)),
}
SCALE_LABELS = {"major": "Mayor", "minor": "Menor", "chromatic": "Cromática"}


@dataclass(frozen=True)
class Key:
    root: int          # 0..11 (C = 0)
    mode: str          # "major" | "minor"
    confidence: float = 0.0

    @property
    def name(self) -> str:
        return f"{NOTE_NAMES[self.root]} {'mayor' if self.mode == 'major' else 'menor'}"

    @property
    def name_es(self) -> str:
        return f"{NOTE_NAMES_ES[self.root]} {'mayor' if self.mode == 'major' else 'menor'}"

    @property
    def short(self) -> str:
        return NOTE_NAMES[self.root] + ("" if self.mode == "major" else "m")

    def pitch_classes(self) -> list[int]:
        return [(self.root + i) % 12 for i in SCALES[self.mode]]

    def relative(self) -> "Key":
        """La relativa (A menor <-> C mayor): mismas notas."""
        if self.mode == "major":
            return Key((self.root + 9) % 12, "minor", self.confidence)
        return Key((self.root + 3) % 12, "major", self.confidence)


def note_index(name: str) -> int | None:
    name = name.strip()
    name = FLAT_ALIASES.get(name, name)
    if name in NOTE_NAMES:
        return NOTE_NAMES.index(name)
    if name in NOTE_NAMES_ES:
        return NOTE_NAMES_ES.index(name)
    return None
