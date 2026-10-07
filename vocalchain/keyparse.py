"""Lectura de tonalidades en texto (Auto-Key "Key/Scale", entrada del usuario, info de canciones).

parse_key_text("A Minor") -> KeyInfo(root=9, minor=True)
Acepta: "A Minor", "Am", "A min", "A#m", "Bb Major", "C# minor", "F♯ Maj", "A Minor (Aeolian)",
        español "La menor", "Do# mayor", "Sol m", "Sib", y devuelve None para
        "Chromatic", "---", "Detecting", "" (Auto-Key antes de detectar).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

NOTE_NAMES_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
NOTE_NAMES_FLAT = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
NOTE_NAMES_ES = ["Do", "Do#", "Re", "Re#", "Mi", "Fa", "Fa#", "Sol", "Sol#", "La", "La#", "Si"]

_LETTER = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}
_SOLFEO = [("do", 0), ("re", 2), ("mi", 4), ("fa", 5), ("sol", 7), ("la", 9), ("si", 11)]
_NO_KEY = ("chromatic", "cromatic", "detect", "listening", "none", "n/a", "---", "--", "?")


@dataclass(frozen=True)
class KeyInfo:
    root: int          # 0 = C ... 11 = B
    minor: bool

    @property
    def name(self) -> str:
        names = NOTE_NAMES_FLAT if self._prefer_flats() else NOTE_NAMES_SHARP
        return f"{names[self.root]} {'Minor' if self.minor else 'Major'}"

    @property
    def short(self) -> str:
        names = NOTE_NAMES_FLAT if self._prefer_flats() else NOTE_NAMES_SHARP
        return names[self.root] + ("m" if self.minor else "")

    @property
    def nombre_es(self) -> str:
        return f"{NOTE_NAMES_ES[self.root]} {'menor' if self.minor else 'mayor'}"

    def relative(self) -> "KeyInfo":
        return KeyInfo((self.root + 3) % 12, False) if self.minor else KeyInfo((self.root + 9) % 12, True)

    def _prefer_flats(self) -> bool:
        return (self.root in (5, 10, 3, 8, 1) and not self.minor) or (self.root in (2, 7, 0, 5, 10) and self.minor)


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def parse_key_text(text: Optional[str]) -> Optional[KeyInfo]:
    """Devuelve KeyInfo o None si el texto no contiene una tonalidad (p.ej. "Chromatic")."""
    if not text:
        return None
    s = _strip_accents(str(text)).strip()
    s = s.replace("♯", "#").replace("♭", "b").replace("♮", "")
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[_/|,;:]+", " ", s).strip()
    low = s.lower()
    if not low or any(low.startswith(w) for w in _NO_KEY):
        return None

    root = None
    rest = ""
    for syl, pc in _SOLFEO:
        if low.startswith(syl) and (len(low) == len(syl) or not low[len(syl)].isalpha()
                                    or low[len(syl):].startswith(("m", "b"))):
            root, rest = pc, s[len(syl):]
            break
    if root is None:
        m = re.match(r"^\s*([A-Ga-g])", s)
        if not m:
            return None
        root, rest = _LETTER[m.group(1).lower()], s[m.end():]

    rest_l = rest.lstrip(" -")
    while True:
        rl = rest_l.lower()
        if rest_l.startswith("#"):
            root += 1; rest_l = rest_l[1:]
        elif rl.startswith(("sharp", "sostenido")):
            root += 1; rest_l = rest_l[9 if rl.startswith("sostenido") else 5:]
        elif rl.startswith(("flat", "bemol")):
            root -= 1; rest_l = rest_l[4 if rl.startswith("flat") else 5:]
        elif rest_l.startswith("b") and not rl.startswith(("bemol",)):
            root -= 1; rest_l = rest_l[1:]
        else:
            break
        rest_l = rest_l.lstrip(" -")
    root %= 12

    word = rest_l.strip(" -.")
    if not word:
        return KeyInfo(root, False)
    first = re.split(r"[\s\-.]+", word)[0]
    if first == "M":
        return KeyInfo(root, False)
    fl = first.lower()
    if fl in ("m", "min", "minor", "menor", "moll", "aeolian", "eolio", "mi", "-") or fl.startswith("minor") \
            or fl.startswith("menor"):
        return KeyInfo(root, True)
    if fl in ("maj", "major", "mayor", "dur", "ionian", "jonico") or fl.startswith(("major", "mayor", "maj")):
        return KeyInfo(root, False)
    if fl.startswith("m"):
        return KeyInfo(root, True)
    return KeyInfo(root, False)


def autotune_key_texts(k: KeyInfo) -> dict:
    """Textos candidatos para Auto-Tune Pro: [2] Key (12 pasos) y [1] Scale (29 pasos)."""
    return {
        "key": [NOTE_NAMES_SHARP[k.root], NOTE_NAMES_FLAT[k.root],
                f"{NOTE_NAMES_SHARP[k.root]}/{NOTE_NAMES_FLAT[k.root]}"],
        "scale": ["Minor", "Natural Minor"] if k.minor else ["Major"],
    }


if __name__ == "__main__":
    import sys
    for t in sys.argv[1:] or ["A Minor", "Am", "Chromatic"]:
        k = parse_key_text(t)
        print(repr(t), "->", k and (k.name, k.short, k.nombre_es))
