; BioAttend for Windows - per-user installer (no administrator rights needed).
; Built by desktop/build_windows.ps1:  makensis /DVERSION=1.0.0 desktop\installer.nsi
Unicode true
SetCompressor /SOLID lzma
RequestExecutionLevel user

!ifndef VERSION
  !define VERSION "1.0.0"
!endif
!define APP "BioAttend"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP}"

Name "${APP} ${VERSION}"
OutFile "..\dist\BioAttend-Setup-${VERSION}.exe"
InstallDir "$LOCALAPPDATA\Programs\${APP}"
InstallDirRegKey HKCU "Software\${APP}" "InstallDir"
BrandingText "${APP} ${VERSION}"

!include "MUI2.nsh"
!define MUI_ICON "bioattend.ico"
!define MUI_UNICON "bioattend.ico"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TEXT "This will install ${APP} ${VERSION} - biometric attendance for ZKTeco devices.$\r$\n$\r$\nEverything runs on this PC: no server is needed. Your data is stored in $LOCALAPPDATA\${APP} and is kept when you update or uninstall."
!define MUI_FINISHPAGE_RUN "$INSTDIR\BioAttend.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Start ${APP} now"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Function StopRunningApp
  ; Ask a running BioAttend to quit (keeps the database consistent); wait only if one was running.
  IfFileExists "$INSTDIR\BioAttend.exe" 0 done
    ExecWait '"$INSTDIR\BioAttend.exe" --quit' $0
    StrCmp $0 "0" 0 done
    Sleep 3000
  done:
FunctionEnd

Function un.StopRunningApp
  IfFileExists "$INSTDIR\BioAttend.exe" 0 done
    ExecWait '"$INSTDIR\BioAttend.exe" --quit' $0
    StrCmp $0 "0" 0 done
    Sleep 3000
  done:
FunctionEnd

Section "BioAttend" SecMain
  SectionIn RO
  Call StopRunningApp
  SetOutPath "$INSTDIR"
  RMDir /r "$INSTDIR\_internal"          ; remove files of the previous version
  File /r "..\dist\BioAttend\*.*"
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  CreateDirectory "$SMPROGRAMS\${APP}"
  CreateShortcut "$SMPROGRAMS\${APP}\${APP}.lnk" "$INSTDIR\BioAttend.exe" "" "$INSTDIR\BioAttend.exe"
  CreateShortcut "$SMPROGRAMS\${APP}\Back up ${APP} now.lnk" "$INSTDIR\BioAttend.exe" "--backup" "$INSTDIR\BioAttend.exe"
  CreateShortcut "$SMPROGRAMS\${APP}\Uninstall ${APP}.lnk" "$INSTDIR\Uninstall.exe"
  CreateShortcut "$DESKTOP\${APP}.lnk" "$INSTDIR\BioAttend.exe" "" "$INSTDIR\BioAttend.exe"

  WriteRegStr HKCU "Software\${APP}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${APP}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "${APP}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\BioAttend.exe"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINST_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  Call un.StopRunningApp
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "${APP}"
  Delete "$DESKTOP\${APP}.lnk"
  RMDir /r "$SMPROGRAMS\${APP}"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "${UNINST_KEY}"
  DeleteRegKey HKCU "Software\${APP}"
  IfSilent keep_data
  MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "Also delete ALL BioAttend data on this PC (employees, attendance, fingerprints, settings and local backups in $LOCALAPPDATA\${APP})?$\r$\n$\r$\nChoose No to keep it for a later reinstall." IDNO keep_data
    RMDir /r "$LOCALAPPDATA\${APP}"
  keep_data:
SectionEnd
