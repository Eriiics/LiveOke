#!/bin/bash
# Construye installer/VocalChain-Setup-<versión>.exe desde Linux (MinGW + NSIS).
# Uso: installer/build_installer.sh [versión]     (requiere engine/VocalEngine.exe ya compilado)
set -e
cd "$(dirname "$0")"
VERSION=${1:-0.3.0}
ROOT=..
STAGE=stage
rm -rf $STAGE && mkdir -p $STAGE/engine

# lanzador VocalChain.exe con icono
cat > $STAGE/launcher.rc <<EOF
1 ICON "vocalchain.ico"
1 VERSIONINFO
FILEVERSION ${VERSION//./,},0
PRODUCTVERSION ${VERSION//./,},0
BEGIN
  BLOCK "StringFileInfo"
  BEGIN
    BLOCK "040A04B0"
    BEGIN
      VALUE "FileDescription", "VocalChain"
      VALUE "ProductName", "VocalChain"
      VALUE "FileVersion", "${VERSION}"
      VALUE "ProductVersion", "${VERSION}"
    END
  END
  BLOCK "VarFileInfo"
  BEGIN
    VALUE "Translation", 0x40A, 1200
  END
END
EOF
cp vocalchain.ico $STAGE/
(cd $STAGE && x86_64-w64-mingw32-windres launcher.rc -O coff -o launcher.res)
x86_64-w64-mingw32-gcc -O2 -municode -mwindows -o $STAGE/VocalChain.exe launcher.c $STAGE/launcher.res -lshell32 -s

# archivos de la app
cp $ROOT/engine/VocalEngine.exe $STAGE/engine/
cp -r $ROOT/vocalchain $STAGE/ && find $STAGE/vocalchain -name __pycache__ -prune -exec rm -rf {} +
cp $ROOT/requirements.txt $ROOT/README.md $STAGE/
for b in setup_env.bat ../restaurar_sonido.bat; do
  sed 's/\r$//; s/$/\r/' "$b" > "$STAGE/$(basename "$b")"   # CRLF para cmd.exe
done
rm -f $STAGE/launcher.rc $STAGE/launcher.res

makensis -V2 -DVERSION=$VERSION -DSTAGE=$STAGE vocalchain.nsi
ls -la VocalChain-Setup-$VERSION.exe
