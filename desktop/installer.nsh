!macro customInstall
  CreateDirectory "$INSTDIR\userdata"
!macroend

!macro customUnInstall
  ; User data and downloaded runtimes deliberately survive uninstall.
!macroend

!macro customRemoveFiles
  SetOutPath "$TEMP"
  RMDir /r "$INSTDIR\resources"
  RMDir /r "$INSTDIR\locales"
  RMDir /r "$INSTDIR\swiftshader"
  Delete "$INSTDIR\${APP_EXECUTABLE_FILENAME}"
  Delete "$INSTDIR\Uninstall 学习助手伙伴.exe"
  Delete "$INSTDIR\*.dll"
  Delete "$INSTDIR\*.pak"
  Delete "$INSTDIR\*.bin"
  Delete "$INSTDIR\icudtl.dat"
  Delete "$INSTDIR\vk_swiftshader_icd.json"
  Delete "$INSTDIR\LICENSE*"
  Delete "$INSTDIR\version"
  Delete "$INSTDIR\uninstallerIcon.ico"
  RMDir "$INSTDIR"
!macroend
