; Instalador de VocalChain (NSIS 3). Se compila con installer/build_installer.sh
; Instala para el usuario actual (sin permisos de administrador) en %LOCALAPPDATA%\Programs\VocalChain,
; prepara el entorno de Python, crea accesos directos (Escritorio + Inicio) y el desinstalador.
; Tus ajustes (~\.vocalchain) y tus grabaciones (Música\VocalChain) no se tocan al desinstalar.

Unicode true
Target amd64-unicode
!include "MUI2.nsh"
!include "LogicLib.nsh"

!define APPNAME "VocalChain"
!ifndef VERSION
  !define VERSION "0.3.0"
!endif
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}"
!ifndef STAGE
  !define STAGE "stage"
!endif

Name "${APPNAME}"
OutFile "VocalChain-Setup-${VERSION}.exe"
InstallDir "$LOCALAPPDATA\Programs\${APPNAME}"
InstallDirRegKey HKCU "Software\${APPNAME}" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
BrandingText "${APPNAME} ${VERSION}"

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "${APPNAME}"
VIAddVersionKey "FileDescription" "Instalador de ${APPNAME}"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" "Chacho"

!define MUI_ICON "${STAGE}\vocalchain.ico"
!define MUI_UNICON "${STAGE}\vocalchain.ico"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "Instalar ${APPNAME} ${VERSION}"
!define MUI_WELCOMEPAGE_TEXT "Cadena vocal en tiempo real, karaoke con letras, auto-tune con la tonalidad de la canción y grabación de tus tomas.$\r$\n$\r$\nSe instalará en tu usuario (no hace falta ser administrador). Al final se prepara el entorno de Python: la primera vez puede tardar unos minutos y verás una ventana negra con el progreso.$\r$\n$\r$\nTus ajustes, presets y grabaciones se conservan."
!define MUI_FINISHPAGE_RUN "$INSTDIR\VocalChain.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Abrir ${APPNAME}"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Spanish"

; --- no sobrescribir mientras la app está abierta -------------------------------------------
!macro WAIT_APP_CLOSED
  wac_loop:
    nsExec::ExecToStack 'cmd /c tasklist /FI "IMAGENAME eq VocalEngine.exe" /NH | find /I "VocalEngine.exe"'
    Pop $0   ; 0 = el motor está abierto
    Pop $1
    StrCmp $0 "0" 0 wac_done
    MessageBox MB_OKCANCEL|MB_ICONEXCLAMATION "VocalChain está abierto. Ciérralo y pulsa Aceptar para continuar." /SD IDCANCEL IDOK wac_loop
    Abort
  wac_done:
!macroend

Function .onInit
  !insertmacro WAIT_APP_CLOSED
FunctionEnd

Section "VocalChain" SecMain
  SetOutPath "$INSTDIR"
  ; versión anterior del código: se reemplaza entera (el .venv se conserva para no reinstalar todo)
  RMDir /r "$INSTDIR\vocalchain"
  RMDir /r "$INSTDIR\engine"

  File "${STAGE}\VocalChain.exe"
  File "${STAGE}\vocalchain.ico"
  File "${STAGE}\requirements.txt"
  File "${STAGE}\setup_env.bat"
  File "${STAGE}\restaurar_sonido.bat"
  File "${STAGE}\README.md"
  File /r "${STAGE}\vocalchain"
  File /r "${STAGE}\engine"

  WriteRegStr HKCU "Software\${APPNAME}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\${APPNAME}" "Version" "${VERSION}"

  ; accesos directos
  CreateShortcut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\VocalChain.exe" "" "$INSTDIR\vocalchain.ico" 0 SW_SHOWNORMAL "" "Cadena vocal y karaoke"
  CreateDirectory "$SMPROGRAMS\${APPNAME}"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" "$INSTDIR\VocalChain.exe" "" "$INSTDIR\vocalchain.ico" 0 SW_SHOWNORMAL "" "Cadena vocal y karaoke"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\Restaurar sonido de Windows.lnk" "$INSTDIR\restaurar_sonido.bat" "" "$SYSDIR\mmsys.cpl" 0 SW_SHOWNORMAL "" "Devuelve la salida de Windows a tus audífonos si la app se cerró mal"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\Carpeta de grabaciones.lnk" "$MUSIC\VocalChain"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\Desinstalar ${APPNAME}.lnk" "$INSTDIR\Desinstalar.exe"
  CreateDirectory "$MUSIC\VocalChain"

  ; desinstalador + entrada en "Aplicaciones instaladas"
  WriteUninstaller "$INSTDIR\Desinstalar.exe"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${APPNAME}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "Chacho"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\vocalchain.ico"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\Desinstalar.exe"'
  WriteRegStr HKCU "${UNINST_KEY}" "URLInfoAbout" "https://github.com/Eriiics/LiveOke"
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" 450000

  ; entorno de Python (ventana visible con el progreso)
  DetailPrint "Preparando el entorno de Python (puede tardar unos minutos la primera vez)..."
  ExecWait '"$SYSDIR\cmd.exe" /c ""$INSTDIR\setup_env.bat""' $0
  ${If} $0 != 0
    MessageBox MB_ICONEXCLAMATION "No se pudo terminar de preparar Python (código $0).$\r$\nVocalChain lo volverá a intentar la próxima vez que lo abras." /SD IDOK
  ${EndIf}
SectionEnd

Function un.onInit
  !insertmacro WAIT_APP_CLOSED
FunctionEnd

Section "Uninstall"
  Delete "$DESKTOP\${APPNAME}.lnk"
  RMDir /r "$SMPROGRAMS\${APPNAME}"
  RMDir /r "$INSTDIR\vocalchain"
  RMDir /r "$INSTDIR\engine"
  RMDir /r "$INSTDIR\.venv"
  Delete "$INSTDIR\VocalChain.exe"
  Delete "$INSTDIR\vocalchain.ico"
  Delete "$INSTDIR\requirements.txt"
  Delete "$INSTDIR\setup_env.bat"
  Delete "$INSTDIR\restaurar_sonido.bat"
  Delete "$INSTDIR\README.md"
  Delete "$INSTDIR\Desinstalar.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${UNINST_KEY}"
  DeleteRegKey HKCU "Software\${APPNAME}"
SectionEnd
