"""Perfiles de voz (trap/rap, voz media) para Auto-Tune Pro + FabFilter Pro-Q 4.

Cada perfil es una lista de pasos (plugin, parámetro, texto). La interfaz los manda al motor con
set_plugin_param_text: para parámetros de lista busca el texto ("Bell", "Alto-Tenor") y para valores
continuos ("90", "+2.5") el motor convierte el número (Hz, dB, %) al valor del plugin.

Nombres/índices tomados de engine.log (7-oct): Auto-Tune Pro y Pro-Q 4 cargados en la app.
Los valores son los del plan v0.3; ajustar con el micrófono y la mezcla real.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

AT = {
    "mode": ("Correction Mode", 0),
    "scale": ("Scale", 1),
    "key": ("Key", 2),
    "retune": ("Retune Speed", 4),
    "tracking": ("Tracking", 9),
    "input": ("Input Type", 10),
    "classic": ("Use Classic Mode DSP", 11),
    "humanize": ("Humanize", 61),
    "vibrato": ("Natural Vibrato", 62),
    "formant": ("Formant Correction", 70),
    "throat": ("Throat Length", 71),
    "transpose": ("Transpose", 74),
}

AUTOTUNE = "Auto-Tune Pro"
EQ_PRE = "Pro-Q 4 (antes)"
EQ_POST = "Pro-Q 4 (después)"


@dataclass
class Step:
    plugin: str
    param: Tuple[str, Optional[int]]
    text: str


@dataclass
class VoiceProfile:
    id: str
    name: str
    emoji: str
    description: str
    steps: List[Step] = field(default_factory=list)
    sends: Dict[str, float] = field(default_factory=dict)   # nivel de envío 0..1 por nombre de bus


def _at(key: str, text: str) -> Step:
    return Step(AUTOTUNE, AT[key], text)


def _eq(slot: str, band: int, what: str, text: str) -> Step:
    return Step(slot, (f"Band {band} {what}", None), text)


def _band(slot: str, band: int, shape: str, freq: float, gain: Optional[float] = None,
          q: Optional[float] = None, slope: Optional[str] = None) -> List[Step]:
    st = [_eq(slot, band, "Used", "Used"), _eq(slot, band, "Enabled", "Enabled"),
          _eq(slot, band, "Shape", shape), _eq(slot, band, "Frequency", f"{freq:g}")]
    if gain is not None:
        st.append(_eq(slot, band, "Gain", f"{gain:+.1f}"))
    if q is not None:
        st.append(_eq(slot, band, "Q", f"{q:g}"))
    if slope:
        st.append(_eq(slot, band, "Slope", slope))
    return st


def _proq_pre() -> List[Step]:
    """Pro-Q 4 ANTES del Auto-Tune: limpiar graves (ayuda a detectar el tono)."""
    return _band(EQ_PRE, 1, "Low Cut", 90, slope="24 dB/oct")


def _proq_post(mud_db: float, presence_db: float, air_db: float) -> List[Step]:
    """Pro-Q 4 DESPUÉS del Auto-Tune: barro, presencia, aire y sibilancia dinámica."""
    s = EQ_POST
    return (_band(s, 1, "Bell", 320, mud_db, 1.2)
            + _band(s, 2, "Bell", 4000, presence_db, 0.8)
            + _band(s, 3, "High Shelf", 11000, air_db)
            + _band(s, 4, "Bell", 7000, 0.0, 2.5)
            + [_eq(s, 4, "Dynamic Range", "-5"), _eq(s, 4, "Dynamics Enabled", "Dynamics Enabled")])


PROFILES: List[VoiceProfile] = [
    VoiceProfile(
        "trap_duro", "Trap duro", "🔥",
        "Efecto robótico marcado. Retune 3, sin humanizar, voz media (Alto-Tenor).",
        [_at("mode", "Auto"), _at("input", "Alto-Tenor"), _at("retune", "3"), _at("humanize", "0"),
         _at("vibrato", "0"), _at("formant", "Off"), _at("tracking", "50")]
        + _proq_pre() + _proq_post(-2.5, +3.0, +2.5),
        {"Reverb": 0.12, "Delay": 0.10},
    ),
    VoiceProfile(
        "melodico", "Melódico", "🎶",
        "Afinado pero cantado. Retune 20, Humanize 25, vibrato natural leve.",
        [_at("mode", "Auto"), _at("input", "Alto-Tenor"), _at("retune", "20"), _at("humanize", "25"),
         _at("vibrato", "2"), _at("formant", "Off"), _at("tracking", "50")]
        + _proq_pre() + _proq_post(-2.0, +2.5, +2.0),
        {"Reverb": 0.22, "Delay": 0.14},
    ),
    VoiceProfile(
        "natural", "Natural / freestyle", "🎤",
        "Corrección suave para rapear/improvisar sin que se note. Retune 45, Humanize 45.",
        [_at("mode", "Auto"), _at("input", "Alto-Tenor"), _at("retune", "45"), _at("humanize", "45"),
         _at("vibrato", "0"), _at("formant", "Off"), _at("tracking", "50")]
        + _proq_pre() + _proq_post(-1.5, +2.0, +1.5),
        {"Reverb": 0.10, "Delay": 0.06},
    ),
]

BY_ID = {p.id: p for p in PROFILES}


def resolve_param(params: List[str], ref: Tuple[str, Optional[int]]) -> Optional[int]:
    """params = nombres del plugin (posición = índice). Busca por nombre exacto, luego normalizado,
    y por último por el índice conocido."""
    name, idx = ref
    norm = lambda s: "".join(ch for ch in s.lower() if ch.isalnum())  # noqa: E731
    for i, p in enumerate(params):
        if p == name:
            return i
    n = norm(name)
    for i, p in enumerate(params):
        if norm(p) == n:
            return i
    if idx is not None and 0 <= idx < len(params):
        return idx
    return None


def apply_profile(profile: VoiceProfile,
                  slots: Dict[str, Tuple[object, List[str]]],
                  send: Callable[[object, int, str], bool]) -> List[str]:
    """slots: {"Auto-Tune Pro": (fx, nombres), "Pro-Q 4 (antes)": ..., "Pro-Q 4 (después)": ...}
    nombres: lista indexada por índice de parámetro del plugin. send(fx, índice, texto) -> aceptado.
    Devuelve avisos (plugin ausente, parámetro no encontrado, texto rechazado)."""
    warnings: List[str] = []
    missing = set()
    for st in profile.steps:
        if st.plugin not in slots:
            missing.add(st.plugin)
            continue
        fx, names = slots[st.plugin]
        i = resolve_param(names, st.param)
        if i is None:
            warnings.append(f"{st.plugin}: no encontré el parámetro '{st.param[0]}'")
            continue
        if not send(fx, i, st.text):
            warnings.append(f"{st.plugin}: '{names[i]}' no aceptó '{st.text}'")
    for p in sorted(missing):
        warnings.insert(0, f"Falta '{p}' en la cadena del micrófono (se saltaron sus ajustes)")
    return warnings
