# 视频下载与字幕工具

面向 Windows 的开源桌面工具，用于下载用户有权处理的 YouTube 视频，并生成英文与中文字幕。程序不需要 API Token；在线翻译不可用时可选用本地翻译模型。

## 普通用户

从 GitHub Releases 下载 `windows-x64.zip`，解压后运行 `VideoSubtitleToolkit.exe`。首次启动进入“组件管理”，安装 yt-dlp、FFmpeg、aria2 和 Node.js。模型不随基础包分发，可按需安装。

基础包体积小，不包含 PyTorch。需要本地 Whisper 转录或离线翻译时，建议使用下方源码安装方式并执行 AI 可选安装；模型仍通过“组件管理”下载。

## 源码运行

需要 64 位 Python 3.11 或 3.12，并在 PowerShell 中执行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup.ps1
.\run.bat
```

安装本地 AI 运行时：

```powershell
.\scripts\setup.ps1 -InstallAI
```

## 数据位置

配置、组件、模型、缓存与日志默认保存在 `%LOCALAPPDATA%\VideoSubtitleToolkit`。可设置环境变量 `VIDEO_SUBTITLE_TOOLKIT_HOME` 改变位置。视频默认输出到当前用户的 `Videos\VideoSubtitleToolkit`，也可在界面选择其他目录。

## 并发与翻译

下载线程和在线翻译线程可在界面调整，程序会依据 CPU 与内存给出上限。自动翻译模式优先使用在线翻译，失败时仅在本地 AI 运行时和翻译模型均可用的情况下回退离线翻译。

## 开发与发布

```powershell
python -m unittest discover -s tests -v
.\scripts\audit_repository.ps1
.\scripts\build_release.ps1 -Version 1.0.0
```

推送 `v*` 标签会触发 GitHub Actions，自动测试、构建 Windows ZIP、生成 SHA-256 并创建 Release。

## 使用责任

请遵守来源网站服务条款、著作权规定和所在地法律。只下载、转录或翻译你有权处理的内容。本项目不绕过 DRM、付费访问或访问控制。

