## Estado (7-oct, tarde): v0.3 integrada

Hecho y probado (motor bajo wine + interfaz offscreen + pytest):
- Pestañas Mezclador · Letra · Grabaciones · Audio. La Letra se despega (⇱) a ventana propia y recuerda
  posición/monitor; al cerrarla vuelve a la pestaña.
- Grabación del mix (lo que suena en el monitor) → MP3 en Música\VocalChain, modo Canción/Sesión, info.txt/json,
  letra.txt, búsqueda en YouTube con confirmación y descarga de la instrumental (yt-dlp). Botón ● REC (Ctrl+R).
- Pestaña Audio: latencia por plugin con avisos (Auto-Tune Low Latency, 48 kHz, buffer 16), CPU por canal,
  multihilo opcional (apagado por defecto).
- Perfiles de voz (Trap duro / Melódico / Natural) para Auto-Tune Pro + Pro-Q 4 y envíos.
- ＋ Mic: segundo micrófono con su cadena; se puede quitar con ✕.
- Auto-Key: se lee el texto único "Key/Scale".
- Ventanas de plugins con título "VocalChain ▸ canal ▸ plugin", color e icono.

Pendiente de verificar en el PC real: grabación con ASIO, valores de Pro-Q 4 aplicados por el perfil,
multihilo con dos cadenas pesadas, descarga con/sin ffmpeg.

# VocalChain — plan para la próxima sesión (v0.3)

Estado: v0.2 funcionando en el PC de Chacho (motor C++/JUCE + interfaz Python). Carpeta:
`C:\Users\ericc\Downloads\vocalchain\vocalchain`. Logs: `C:\Users\ericc\.vocalchain\engine.log` y `diag.log`.
Notas técnicas completas: doc del proyecto `claude/vocalchain-notas.md`.

## Hallazgos de los registros (7-oct, ~2:30)
- ASIO Focusrite a **44.1 kHz, buffer 16** → latencia de la interfaz **5.1 ms**, CPU 6-7 %, proc_max 0.1-0.2 ms.
  Pero hay **~3800 bloques "tarde"**: con buffer 16 el procesamiento a veces no llega → subir a 32/64 o
  revisar la medición (umbral 80 % de 0.36 ms es muy estricto).
- **Auto-Tune Pro reporta 2670 muestras de latencia (≈60 ms a 44.1 k)** ← esta es la causa principal del delay
  que se siente, no la interfaz. Su modo *Low Latency* no es un parámetro VST (es un botón de su interfaz):
  activarlo en la ventana del plugin (el estado se guarda en la sesión) y verificar en engine.log que baje.
  Revisar también "Use Classic Mode DSP" [11] (el modo clásico tiene menos latencia).
- VB-Cable a **48 kHz** y la interfaz a **44.1 kHz** → ratio 1.088 (re-muestreo constante + 14.8 ms de colchón).
  Poner todo a 48 kHz (o todo a 44.1) y bajar el colchón.
- **clips=140** en la entrada del mic → el gain de la Focusrite está alto en momentos fuertes.
- Auto-Key 2 expone **[2] "Key/Scale"** (decía "Chromatic" hasta detectar) → `parse_key_text` debe aceptar
  un solo texto tipo "A Minor"/"Am". Hay que confirmar el formato cuando detecte.
- Parámetros útiles de Auto-Tune Pro: [0] Correction Mode, [1] Scale (29 pasos), [2] Key (12),
  [4] Retune Speed, [9] Tracking, [10] Input Type (Alto-Tenor…), [61] Humanize, [62] Natural Vibrato,
  [70] Formant Correction, [71] Throat Length, [74] Transpose. Pro-Q 4 también carga bien.

## Lo que pidió (respuestas con botones)
1. **Grabación**: grabar *solo la mezcla* (lo mismo que escucha), **MP3**, en `Música\VocalChain\`.
   - Modo **Canción**: carpeta por canción (`Música\VocalChain\<Instrumental> - <fecha>\`).
   - Modo **Sesión**: **un archivo continuo** con marcas de cada canción (lista de tiempos en la info).
   - Info guardada (`info.json` + `info.txt` legible): nombre de la instrumental, link de YouTube,
     artista/canal, tonalidad detectada, BPM (cuando exista), fecha, duración, cadena/preset usado,
     letra propia (archivo `letra.txt` editable), notas. Botón **"Descargar instrumental"** (yt-dlp → mp3
     en la misma carpeta) y botón "Abrir carpeta".
   - El link: sacarlo del título de la pestaña/sesión multimedia no es posible directo → opciones a
     implementar: campo para pegar el link + buscarlo solo con yt-dlp `ytsearch1:<título>` y pedir confirmación.
2. **Delay**: Low Latency de Auto-Tune, igualar frecuencias, buffer 32, medir latencia real ida-vuelta
   (botón de prueba: pulso por la salida → entrada loopback), mostrar latencia por plugin en la cadena.
3. **Ventanas**: letra y ajustes como **pestañas dentro de la ventana principal**; la pestaña de **Letra se puede
   despegar** (botón ⇱ o arrastrar) a una ventana flotante para el otro monitor y volver a acoplarla
   (recordar monitor/posición/pantalla completa); iconos/título distintivo
   para las ventanas de plugins (las abre el motor: setIcon + color en la barra de título JUCE).
4. **Perfil de voz** (trap/rap, voz **media**, quiere **varios perfiles**) con Auto-Tune Pro + FabFilter Pro-Q 4:
   - "Trap duro": Retune 0-5, Humanize 0, Flex-Tune 0, Input Type Alto-Tenor.
   - "Melódico": Retune 15-25, Humanize 20-30, Natural Vibrato leve.
   - "Natural/freestyle": Retune 35-50, Humanize 40+.
   - Pro-Q 4 (antes del autotune: HPF 80-100 Hz; después: corte suave 250-400 Hz si suena "barroso",
     presencia +2-3 dB 3-5 kHz, aire en 10-12 kHz, dinámico en sibilancia 6-8 kHz si no se usa De-Esser).
   - Ajustar con datos reales: falta modelo de micrófono y la conversación de mezcla en FL Studio.

5. **Dos micrófonos + multihilo** (pedido 7-oct 03:26):
   - Botón "＋ Mic 2" que crea un segundo canal de entrada ASIO (input 2 de la Scarlett) con **su propia cadena**
     (puede tener otro Auto-Tune/Pro-Q), sus envíos y su fader. Las dos voces se pueden grabar en la mezcla.
   - Opción **"Procesamiento multihilo"** en ⚙ Audio (desactivada por defecto): cuando está activa, el motor
     procesa las cadenas de los canales de entrada en paralelo en hilos de trabajo de tiempo real
     (pool fijo creado al iniciar, prioridad alta, sincronizado con spin/atomics sin bloqueos ni reservas de
     memoria; el hilo ASIO reparte los canales, espera a que terminen y después suma buses/salidas).
     Usarlo solo si con 2 cadenas pesadas (2× Auto-Tune Pro) aparecen cortes; con 1 cadena no aporta.
   - Mostrar en la barra la CPU por canal y si el multihilo está activo; probar con buffer 32/64.

## Implementación prevista
- **Motor (C++)**: `Recorder` en el hilo de audio → FIFO lock-free → hilo escritor (WAV temporal 32f) →
  al parar, convertir a MP3 (LAME vía `lameenc` en Python, o ffmpeg si está). Comandos `rec_start {path,mode}`,
  `rec_marker {name}`, `rec_stop` → devuelve duración y marcas. Graba la salida Monitor (lo que escucha).
- **Interfaz**: pestaña "🎙 Grabar" (modo Canción/Sesión, botón ● REC, tiempo, campos de la instrumental,
  link, descargar, historial de grabaciones con reproducir/abrir carpeta). Pestañas: Mezclador · Letra · Grabaciones · Audio.
- **Perfiles**: presets de cadena (JSON con estado de plugins) + selector rápido en la barra.
- Dependencias nuevas: `yt-dlp`, `lameenc` (MP3 sin instalar ffmpeg).

## Pendiente de Chacho
- Pegar la conversación de mezcla en FL Studio (o un resumen) en el proyecto "Karaoke Freestyle" de claude.ai.
- Decir el modelo de micrófono y la generación de la Scarlett 2i2 (3ª/4ª).
- Probar Low Latency en Auto-Tune Pro y contar si baja el delay.
