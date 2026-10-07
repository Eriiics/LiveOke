#!/bin/bash
# Compila VocalEngine.exe para Windows desde Linux (MinGW-w64 + JUCE 7.0.12).
# Uso: ./build_mingw.sh  (JUCE y el SDK de ASIO se buscan en $JUCE_DIR y $ASIO_DIR)
set -e
cd "$(dirname "$0")"
JUCE_DIR=${JUCE_DIR:-/tmp/claude-0/juce7}
ASIO_DIR=${ASIO_DIR:-/tmp/claude-0/asiosdk}
J=$JUCE_DIR/modules
CXX=x86_64-w64-mingw32-g++-posix
OUT=build/obj
mkdir -p $OUT
FLAGS="-std=c++17 -O2 -DNDEBUG -DUNICODE -D_UNICODE -D_WIN32_WINNT=0x0A00 -DWINVER=0x0A00 -Isrc -I$J -I$ASIO_DIR/common \
 -I$J/juce_audio_processors/format_types/VST3_SDK -Wno-attributes -fpermissive -w"
MODS="core events data_structures graphics gui_basics gui_extra audio_basics audio_devices audio_formats audio_processors audio_utils dsp"
pids=()
for m in $MODS; do
  src=build/mod_$m.cpp
  [ -f $src ] || printf '#include "JuceConfig.h"\n#include <juce_%s/juce_%s.cpp>\n' $m $m > $src
  o=$OUT/mod_$m.o
  if [ ! -f $o ] || [ src/JuceConfig.h -nt $o ]; then
    ( $CXX $FLAGS -c $src -o $o && echo "ok $m" ) &
    pids+=($!)
    if [ ${#pids[@]} -ge ${JOBS:-2} ]; then wait ${pids[0]} || { echo "ERROR en $m"; exit 1; }; pids=("${pids[@]:1}"); fi
  fi
done
for p in "${pids[@]}"; do wait $p || { echo "ERROR de compilación"; exit 1; }; done
pids=()
for f in Engine Main Recorder; do
  ( $CXX $FLAGS -Wall -c src/$f.cpp -o $OUT/$f.o && echo "ok $f" ) &
  pids+=($!)
done
fail=0
for p in "${pids[@]}"; do wait $p || fail=1; done
[ $fail = 0 ] || { echo "ERROR de compilación"; exit 1; }
$CXX -o build/VocalEngine.exe $OUT/*.o -static -static-libgcc -static-libstdc++ -mwindows -s \
  -lwinmm -lole32 -loleaut32 -luuid -lws2_32 -lversion -lshlwapi -lwininet -limm32 -lcomdlg32 -lgdi32 \
  -lcomctl32 -lshell32 -luser32 -lkernel32 -ladvapi32 -liphlpapi -lrpcrt4 -lsetupapi -lcrypt32 -ldxgi \
  -ld2d1 -ldwrite -lopengl32 -lwsock32 -lksuser
ls -la build/VocalEngine.exe
