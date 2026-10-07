"""Devuelve la salida de Windows al dispositivo que había antes de abrir VocalChain.
Úsalo (restaurar_sonido.bat) si la app se cerró de golpe y Windows quedó sonando por VB-Cable."""
import json
from pathlib import Path

from . import winvolume as wv

settings = Path.home() / ".vocalchain" / "ui_settings.json"
try:
    data = json.loads(settings.read_text(encoding="utf-8"))
except Exception:
    data = {}
prev = data.get("restore_output")
if prev:
    wv.set_default_render(prev[0])
    data.pop("restore_output", None)
    settings.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Salida de Windows devuelta a: {prev[1]}")
else:
    # sin registro: elegir la primera salida que no sea un cable virtual
    for dev_id, name in wv.list_render_devices():
        if "cable" not in name.lower() and "virtual" not in name.lower():
            wv.set_default_render(dev_id)
            print(f"Salida de Windows puesta en: {name}")
            break
