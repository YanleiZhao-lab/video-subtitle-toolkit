# 本机环境运行指南

本文面向 Windows 10/11，优先使用命令行完成下载、安装、运行、测试、打包和发布。普通用户建议直接运行 GitHub Release；需要 Whisper 本地转录或离线翻译时，再使用源码环境。

## 1. 已验证环境

以下版本是 2026-09-20 的已验证基线，不代表所有组件的最低版本：

| 组件 | 已验证版本 | 用途 |
| --- | --- | --- |
| Windows | Windows 11 x64 | 运行平台 |
| PowerShell | 7.6.5 | 安装、测试和打包 |
| Git | 2.39.1.windows.1 | 获取和提交源码 |
| Python | 3.12.6 x64 | 源码运行与 AI 扩展 |
| GitHub CLI | 2.101.0 | 下载 Release、创建仓库和发布 |
| Node.js / npm | 24.13.0 / 11.13.0 | 安装浏览器自动化 CLI；不是基础运行必需项 |
| agent-browser | 0.38.1 | 可选的浏览器自动化维护工具 |
| Microsoft Edge | 154.0.4258.24 | 可选的浏览器自动化引擎 |

项目要求 Python 3.11 或 3.12。不要使用 32 位 Python。

## 2. 直接运行 Release

这种方式不需要预装 Python，适合普通用户。

### 2.1 安装 GitHub CLI

以普通 PowerShell 窗口执行：

```powershell
winget install --id GitHub.cli --exact --source winget `
  --accept-source-agreements --accept-package-agreements
```

关闭并重新打开 PowerShell，然后检查：

```powershell
gh --version
```

### 2.2 下载最新版本

```powershell
$releaseDir = Join-Path $PWD 'video-subtitle-toolkit-release'
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null

gh release download v1.0.3 `
  --repo YanleiZhao-lab/video-subtitle-toolkit `
  --pattern '*.zip' `
  --pattern '*.sha256' `
  --dir $releaseDir `
  --clobber
```

公开仓库的 Release 下载不要求 GitHub 登录。如需登录，可执行：

```powershell
gh auth login --hostname github.com --git-protocol https --web
gh auth status
```

### 2.3 校验下载文件

```powershell
$zip = Join-Path $releaseDir 'video-subtitle-toolkit-1.0.3-windows-x64.zip'
$checksumFile = "$zip.sha256"
$actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLower()
$expected = ((Get-Content -LiteralPath $checksumFile -Encoding ASCII) -split '\s+')[0].ToLower()

if ($actual -ne $expected) {
    throw "SHA-256 mismatch: expected $expected, actual $actual"
}

Write-Host "SHA-256 verified: $actual" -ForegroundColor Green
```

### 2.4 解压并启动

```powershell
$installDir = Join-Path $PWD 'VideoSubtitleToolkit'
Expand-Archive -LiteralPath $zip -DestinationPath $installDir -Force
Start-Process -FilePath (Join-Path $installDir 'VideoSubtitleToolkit.exe')
```

首次启动后，通过“组件管理”按需安装 yt-dlp、FFmpeg、aria2、Node.js 和模型。所有下载项均执行 SHA-256 校验。

如果运行后不断出现新窗口，说明仍在使用 `v1.0.2` 或更早版本。先结束旧进程，再安装 `v1.0.3` 或更高版本：

```powershell
Get-Process -Name VideoSubtitleToolkit -ErrorAction SilentlyContinue | Stop-Process -Force
```

## 3. 源码环境

源码方式适合开发、调试、本地 Whisper 转录和离线翻译。

### 3.1 安装基础工具

```powershell
winget install --id Git.Git --exact --source winget `
  --accept-source-agreements --accept-package-agreements

winget install --id Python.Python.3.12 --exact --source winget `
  --accept-source-agreements --accept-package-agreements

winget install --id Microsoft.PowerShell --exact --source winget `
  --accept-source-agreements --accept-package-agreements
```

重新打开 PowerShell 7，验证环境：

```powershell
git --version
python --version
python -c 'import struct, tkinter; print("%d-bit; Tk %s" % (struct.calcsize("P") * 8, tkinter.TkVersion))'
```

Python 输出必须为 `3.11.x` 或 `3.12.x`，并且必须显示 `64-bit`。

### 3.2 克隆仓库

```powershell
gh repo clone YanleiZhao-lab/video-subtitle-toolkit
Set-Location .\video-subtitle-toolkit
git status --short
```

正常情况下，`git status --short` 不输出任何内容。

### 3.3 创建基础虚拟环境

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup.ps1
```

脚本会完成以下操作：

1. 检查 Python 3.11/3.12。
2. 创建项目私有的 `.venv`。
3. 安装基础依赖。
4. 执行安装验证。
5. 运行全部单元测试。

使用 CLI 启动：

```powershell
.\.venv\Scripts\pythonw.exe -B .\app\video_tool.py
```

需要在控制台查看异常时，使用：

```powershell
.\.venv\Scripts\python.exe -B .\app\video_tool.py
```

### 3.4 安装本地 AI 运行时

Whisper、PyTorch、Transformers 和 SentencePiece 体积较大，只在确实需要本地转录或离线翻译时安装：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup.ps1 -InstallAI
```

完成后启动程序，在“组件管理”中安装：

- `Whisper small.en 模型`
- `离线中英翻译模型`
- yt-dlp、FFmpeg、aria2 和 Node.js

该流程不需要 API Token。本地模型安装完成后，可在断开在线翻译服务的情况下运行离线翻译。

## 4. 数据目录

默认应用数据目录：

```text
%LOCALAPPDATA%\VideoSubtitleToolkit
```

其中保存配置、工具、模型、缓存、元数据、字幕中间结果和日志。默认视频输出目录为：

```text
%USERPROFILE%\Videos\VideoSubtitleToolkit
```

临时指定独立数据目录：

```powershell
$env:VIDEO_SUBTITLE_TOOLKIT_HOME = 'D:\VideoSubtitleToolkitData'
.\.venv\Scripts\pythonw.exe -B .\app\video_tool.py
```

永久写入当前用户环境变量：

```powershell
[Environment]::SetEnvironmentVariable(
    'VIDEO_SUBTITLE_TOOLKIT_HOME',
    'D:\VideoSubtitleToolkitData',
    'User'
)
```

## 5. 测试与隐私审计

在仓库根目录执行：

```powershell
$env:PYTHONPATH = (Resolve-Path .\app)
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\scripts\audit_repository.ps1
git diff --check
git status --short
```

预期结果：

- 31 项或更多测试全部通过。
- 隐私与大文件审计通过。
- `git diff --check` 无错误。
- 未修改文件时 `git status --short` 无输出。

## 6. 本机打包

安装开发依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r .\requirements-dev.txt
```

构建 Windows 轻量包：

```powershell
.\scripts\build_release.ps1 -Version 1.0.3
```

输出位于：

```text
release\video-subtitle-toolkit-1.0.3-windows-x64.zip
release\video-subtitle-toolkit-1.0.3-windows-x64.zip.sha256
```

打包脚本会在构建前清理固定的 `build`、`dist` 和 `release` 目录；不要把个人文件放进这些目录。

## 7. GitHub CLI 发布

仅项目维护者需要执行本节。

```powershell
gh auth login --hostname github.com --git-protocol https --web
gh auth status
git push origin main

$version = 'v1.0.4'
git tag -a $version -m "Video Subtitle Toolkit $version"
git push origin $version
```

查看自动化状态：

```powershell
gh run list --repo YanleiZhao-lab/video-subtitle-toolkit --limit 10
gh run watch --repo YanleiZhao-lab/video-subtitle-toolkit
gh release view $version --repo YanleiZhao-lab/video-subtitle-toolkit
```

推送 `v*` 标签后，GitHub Actions 会自动运行测试、隐私审计、PyInstaller 构建、ZIP 打包、SHA-256 生成和 Release 上传。

## 8. 可选浏览器自动化 CLI

浏览器自动化不是程序运行依赖，仅用于维护网页流程。

```powershell
winget install --id OpenJS.NodeJS --exact --source winget `
  --accept-source-agreements --accept-package-agreements
npm.cmd install -g agent-browser
agent-browser --version
```

指定本机 Edge：

```powershell
$env:AGENT_BROWSER_EXECUTABLE_PATH = `
  'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
agent-browser --session github-edge --headed open https://github.com
```

结束会话：

```powershell
agent-browser --session github-edge close
```

不要把浏览器状态文件、Cookie、GitHub Token 或授权信息提交到仓库。

## 9. 常见问题

### PowerShell 禁止执行脚本

只对当前窗口临时放行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

### 找不到 `gh`、`python` 或 `agent-browser`

安装后关闭并重新打开终端，然后执行：

```powershell
Get-Command gh, python, git, agent-browser -ErrorAction SilentlyContinue
```

若 npm 全局命令被 PowerShell 执行策略拦截，可使用 `agent-browser.cmd` 或 `npm.cmd`。

### 中文日志显示乱码

优先使用 PowerShell 7，并设置：

```powershell
$env:PYTHONUTF8 = '1'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
```

程序 GUI 日志使用 Unicode，不依赖终端代码页。

### 在线翻译不可用

在线翻译可能受到网络限制或频率限制。自动模式会在本地 AI 运行时和翻译模型均已安装时回退到离线翻译；否则应先执行 `.\scripts\setup.ps1 -InstallAI` 并在组件管理器中安装离线模型。
