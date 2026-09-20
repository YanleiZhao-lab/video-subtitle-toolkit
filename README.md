# Video Subtitle Toolkit

A lightweight Windows desktop app for downloading YouTube videos that you are authorized to use and producing English and Chinese subtitles. It requires no API token. Large media tools and models are installed only when requested.

## Windows release

Download the `windows-x64.zip` asset from GitHub Releases, extract it, and run `VideoSubtitleToolkit.exe`. Open **Component Manager** on first launch to install yt-dlp, FFmpeg, aria2, and Node.js. Models are optional downloads.

In the packaged release, base tools are installed into `tools` beside `VideoSubtitleToolkit.exe`, not `_internal`. Keep the entire extracted folder when moving to another PC. Extract to a writable location; protected directories such as `Program Files` prevent component installation. Tools installed by earlier releases under `%LOCALAPPDATA%\VideoSubtitleToolkit\tools` remain untouched and are not implicitly reused by the new portable release.

The base package intentionally excludes PyTorch. For local Whisper transcription and offline translation, use the source installation with the AI option below, then install the models from Component Manager.

## Run from source

Install 64-bit Python 3.11 or 3.12, then run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup.ps1
.\run.bat
```

For the optional local AI runtime:

```powershell
.\scripts\setup.ps1 -InstallAI
```

Configuration, models, cache and logs default to `%LOCALAPPDATA%\VideoSubtitleToolkit`. Source-mode tools are stored there too. Override this user-data location with `VIDEO_SUBTITLE_TOOLKIT_HOME`; it does not change the packaged release's base-tool location.

## Build and test

```powershell
python -m unittest discover -s tests -v
.\scripts\audit_repository.ps1
.\scripts\build_release.ps1 -Version 1.0.0
```

Tags matching `v*` trigger the GitHub Actions release workflow, which creates a Windows ZIP and SHA-256 checksum.

## Responsible use

Follow the source site's terms, copyright rules, and applicable law. Process only content you are allowed to use. This project does not bypass DRM, paywalls, or access controls.

Chinese documentation: [README.zh-CN.md](README.zh-CN.md)
