# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

root = Path(SPECPATH)
app = root / "app"

a = Analysis(
    [str(app / "video_tool.py")],
    pathex=[str(app)],
    binaries=[],
    datas=[
        (str(app / "dependencies.json"), "."),
        (str(app / "build_video_subtitles.py"), "."),
        (str(app / "translate_video_captions.py"), "."),
        (str(app / "assets"), "assets"),
    ],
    hiddenimports=[],
    excludes=["torch", "whisper", "transformers", "sentencepiece"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VideoSubtitleToolkit",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(app / "assets" / "video-tool.ico"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name="VideoSubtitleToolkit",
)

