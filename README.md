# VocalChain 🎤

Mesa de mezcla para cantar sobre lo que suena en el PC, con latencia mínima:
cadena vocal con plugins VST3 (Auto-Tune Pro, etc.), buses de reverb/delay, sidechain, la
tonalidad de la canción puesta sola en el Auto-Tune, y la letra en grande.

## Cómo está hecho

| Parte | Qué hace | Dónde |
|---|---|---|
| **VocalEngine.exe** (C++ / JUCE) | Todo el audio: ASIO de la Focusrite, efectos, plugins VST3, ruteo, música de VB-Cable | `engine/` |
| **Interfaz** (Python / PySide6) | Mezclador, cadena, ajustes, tonalidad, canción actual y letras | `vocalchain/` |

La interfaz abre el motor sola y le habla por `127.0.0.1:47800`. Si un plugin hace caer el motor,
la interfaz lo avisa y con **⏻ Motor** se vuelve a abrir (la sesión se guarda sola).

## Uso

1. Doble clic en **`run.bat`**.
2. La primera vez pregunta si agregar **Auto-Tune Pro** al micrófono y pone **Auto-Key** en el canal PC.
3. **⚙ Audio**: Driver ASIO → *Focusrite USB ASIO*, buffer **64** (sube a 128 si oyes crujidos), 48 kHz.
4. En la tira **Mic** elige la entrada de la Focusrite donde está el micrófono.

### La música del PC (VB-Cable)
Al abrir, la app pone **CABLE Input** como salida de Windows y la música entra al motor por
*CABLE Output*. Así el fader **PC** es un fader real y el **sidechain** puede bajar la música cuando cantas.
Al cerrar, Windows vuelve a su salida anterior. Si la app se cerró de golpe y no hay sonido en Windows:
**`restaurar_sonido.bat`**.

Para que la música llegue con poco retraso:
- `C:\Program Files\VB\CABLE\VBCABLE_ControlPanel.exe` → **Max Latency: 1024 o 2048**.
- Windows → Sonido → CABLE Input y CABLE Output → Opciones avanzadas → **48000 Hz**.
- En ⚙ Audio, *Colchón VB-Cable* 10 ms (súbelo si la música se corta).

### Tonalidad y Auto-Tune
La app detecta la tonalidad de lo que suena (y además lee **Auto-Key** si está en el canal PC) y
la pone en los parámetros *Key/Scale* de Auto-Tune Pro. Se puede fijar a mano arriba.

### Letras
Busca en orden: **tus archivos** (`%USERPROFILE%\.vocalchain\lyrics\Artista - Canción.lrc` o `.txt`),
**LRCLIB**, **Musixmatch**, **NetEase**, **Megalobiz** (sincronizadas), y **Genius** / **lyrics.ovh** (texto).
**⟳ Otra fuente** prueba la siguiente; **📁** abre la carpeta de letras propias.

## Archivos

`%USERPROFILE%\.vocalchain\`: `engine_session.json` (mezcla + estado de plugins), `ui_settings.json`,
`engine.log` y `diag.log` (diagnóstico), `presets\`, `lyrics\`, `lyrics_cache\`.

## Modificar

- **Efecto nativo nuevo**: clase en `engine/src/Dsp.h` + registrarla en `createBuiltin()`.
  La interfaz genera sus controles sola.
- **Compilar el motor**: `engine/build_mingw.sh` (Linux/WSL, MinGW + JUCE 7.0.12) o
  `engine/CMakeLists.txt` con Visual Studio 2022 (sin probar todavía).
- **Interfaz**: `vocalchain/ui/`. Protocolo con el motor: `vocalchain/remote.py`.
- Los archivos `engine.py`, `effects.py`, `autotune.py` de `vocalchain/` son del motor anterior en
  Python; ya no se usan.
