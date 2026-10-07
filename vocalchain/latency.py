"""Latencia de la cadena: cuánto agrega cada plugin y avisos para la interfaz."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional

WARN_MS = 10.0     # un plugin que agrega más de esto se nota al cantar
TOTAL_WARN_MS = 15.0


@dataclass
class PluginLatency:
    name: str
    samples: int

    def ms(self, sr: float) -> float:
        return 1000.0 * self.samples / sr


def fmt_ms(ms: float) -> str:
    return f"{ms:.1f} ms" if ms < 100 else f"{ms:.0f} ms"


def plugin_hint(name: str, samples: int, sr: float) -> Optional[str]:
    ms = 1000.0 * samples / sr
    n = name.lower()
    if "auto-tune" in n or "autotune" in n:
        if samples > 1024:
            return (f"Auto-Tune agrega {fmt_ms(ms)}. Abre su ventana y activa «Low Latency» (botón en la "
                    "barra superior de Auto-Tune Pro); el estado queda guardado en la sesión. Si sigue alto, "
                    "prueba «Use Classic Mode DSP».")
        return None
    if "pro-q" in n and ms > WARN_MS:
        return (f"Pro-Q agrega {fmt_ms(ms)}: está en modo Linear Phase o Natural Phase. Para voz en vivo "
                "usa «Zero Latency» (abajo a la izquierda en Pro-Q).")
    if ms > WARN_MS:
        return f"{name} agrega {fmt_ms(ms)}. Busca un modo «low latency/live» en el plugin o sácalo en vivo."
    return None


def chain_report(plugins: Iterable[PluginLatency], sr: float, buffer_size: int,
                 io_latency_ms: Optional[float] = None, pc_sr: Optional[float] = None) -> dict:
    plugins = list(plugins)
    total_plugins = sum(p.samples for p in plugins)
    buf_ms = 1000.0 * buffer_size / sr
    io_ms = io_latency_ms if io_latency_ms is not None else 2 * buf_ms
    total_ms = io_ms + 1000.0 * total_plugins / sr
    rows = [{"name": p.name, "samples": p.samples, "ms": round(p.ms(sr), 2),
             "level": "bad" if p.ms(sr) > WARN_MS else ("warn" if p.ms(sr) > 3 else "ok")} for p in plugins]
    hints: List[str] = [h for p in plugins if (h := plugin_hint(p.name, p.samples, sr))]
    if pc_sr and abs(pc_sr - sr) > 1:
        hints.append(f"La Scarlett está a {sr/1000:g} kHz y VB-Cable a {pc_sr/1000:g} kHz: el motor re-muestrea "
                     "y necesita más colchón. Pon los dos a 48 kHz (Focusrite Control y Panel de sonido → "
                     "CABLE Input/Output → Propiedades → Opciones avanzadas).")
    if 0 < buffer_size <= 16:
        hints.append("Buffer 16: deja menos de 0.4 ms para procesar cada bloque; con Auto-Tune Pro es fácil "
                     "que haya bloques tarde/cortes. Recomendado 32 (o 64 si hay chasquidos).")
    if total_ms > TOTAL_WARN_MS:
        hints.insert(0, f"Latencia total estimada {fmt_ms(total_ms)} (interfaz {fmt_ms(io_ms)} + plugins "
                        f"{fmt_ms(1000.0*total_plugins/sr)}). Por encima de ~15 ms se siente el eco al cantar.")
    return {"rows": rows, "total_ms": round(total_ms, 1), "io_ms": round(io_ms, 1),
            "plugins_ms": round(1000.0 * total_plugins / sr, 1), "hints": hints}


def late_block_budget(sr: float, buffer_size: int, threshold: float = 0.8) -> float:
    return threshold * 1000.0 * buffer_size / sr
