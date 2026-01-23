from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

# Only collect essential Qt plugins
# Exclude: 3D, multimedia, web, SQL, QML, permissions, etc.
exclude_patterns = [
    "*location*",
    "*position*",
    "*3d*",
    "*assetimport*",
    "*scene*",
    "*geometry*",
    "*render*",
    "*qml*",
    "*quick*",
    "*designer*",
    "*sql*",
    "*webview*",
    "*webengine*",
    "*multimedia*",
    "*audio*",
    "*video*",
    "*bluetooth*",
    "*nfc*",
    "*texttospeech*",
    "*pdf*",
    "*sensors*",
    "*serial*",
    "*permissions*",
    "*tls*",
]

datas = collect_data_files("PyQt6", subdir="Qt6/plugins", excludes=exclude_patterns)
binaries = collect_dynamic_libs("PyQt6", search_patterns=["*.dylib", "*.so"])
