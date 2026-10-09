/* VocalChain.exe — lanzador de la app instalada.
 * Abre  .venv\Scripts\pythonw.exe -m vocalchain  desde la carpeta de instalación (sin consola).
 * Si falta el entorno de Python, ejecuta setup_env.bat (con ventana, para ver el progreso) y reintenta.
 * Si la app se cierra con error en los primeros segundos, ofrece abrir el registro (app.log). */
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <shlobj.h>
#include <wchar.h>

static wchar_t appDir[MAX_PATH];

static int fileExists(const wchar_t* p) {
    DWORD a = GetFileAttributesW(p);
    return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY);
}

static DWORD runAndWait(wchar_t* cmd, DWORD flags, DWORD waitMs, DWORD* exitCode) {
    STARTUPINFOW si; PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof si); si.cb = sizeof si;
    ZeroMemory(&pi, sizeof pi);
    if (!CreateProcessW(NULL, cmd, NULL, NULL, FALSE, flags, NULL, appDir, &si, &pi))
        return GetLastError();
    DWORD w = WaitForSingleObject(pi.hProcess, waitMs);
    if (exitCode) {
        *exitCode = STILL_ACTIVE;
        if (w == WAIT_OBJECT_0) GetExitCodeProcess(pi.hProcess, exitCode);
    }
    CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
    return 0;
}

int WINAPI wWinMain(HINSTANCE h, HINSTANCE p, PWSTR args, int show) {
    (void) h; (void) p; (void) args; (void) show;
    GetModuleFileNameW(NULL, appDir, MAX_PATH);
    wchar_t* slash = wcsrchr(appDir, L'\\');
    if (slash) *slash = 0;

    wchar_t pyw[MAX_PATH + 64];
    swprintf(pyw, MAX_PATH + 64, L"%ls\\.venv\\Scripts\\pythonw.exe", appDir);

    if (!fileExists(pyw)) {
        wchar_t cmd[MAX_PATH * 2];
        swprintf(cmd, MAX_PATH * 2, L"cmd.exe /c \"\"%ls\\setup_env.bat\"\"", appDir);
        DWORD code = 0;
        if (runAndWait(cmd, CREATE_NEW_CONSOLE, INFINITE, &code) != 0 || !fileExists(pyw)) {
            MessageBoxW(NULL, L"No se pudo preparar el entorno de Python.\n\n"
                              L"Instala Python 3.12 desde python.org (marca \"Add to PATH\") "
                              L"y vuelve a abrir VocalChain.", L"VocalChain", MB_ICONERROR);
            return 1;
        }
    }

    wchar_t cmd[MAX_PATH * 2];
    swprintf(cmd, MAX_PATH * 2, L"\"%ls\" -m vocalchain", pyw);
    DWORD code = 0;
    DWORD err = runAndWait(cmd, 0, 6000, &code);
    if (err != 0) {
        MessageBoxW(NULL, L"No se pudo abrir VocalChain (pythonw.exe).", L"VocalChain", MB_ICONERROR);
        return 1;
    }
    if (code != STILL_ACTIVE && code != 0) {
        wchar_t logp[MAX_PATH];
        if (SUCCEEDED(SHGetFolderPathW(NULL, CSIDL_PROFILE, NULL, 0, logp))) {
            wcscat(logp, L"\\.vocalchain\\app.log");
            if (MessageBoxW(NULL, L"VocalChain se cerró con un error al iniciar.\n¿Abrir el registro (app.log)?",
                            L"VocalChain", MB_ICONWARNING | MB_YESNO) == IDYES)
                ShellExecuteW(NULL, L"open", logp, NULL, NULL, SW_SHOWNORMAL);
        }
        return (int) code;
    }
    return 0;
}
