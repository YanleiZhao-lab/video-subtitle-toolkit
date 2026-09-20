from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import parse_qs, urlencode, urlparse


DEFAULT_CONFIG = {
    "output_directory": "",
    "whisper_model": "small.en",
    "download_video": True,
    "english_subtitles": True,
    "chinese_subtitles": True,
    "validate_outputs": True,
    "translation_mode": "auto",
    "download_workers": 2,
    "translation_workers": 2,
    "download_retries": 10,
    "concurrent_fragments": 4,
    "minimum_free_gb": 2,
    "python_path": "",
    "node_path": "",
}

SUPPORTED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
SRT_CUE_PATTERN = re.compile(
    r"(?m)^(\d+)\s*\n"
    r"(\d\d:\d\d:\d\d,\d{3}) --> (\d\d:\d\d:\d\d,\d{3})\s*\n"
)


@dataclass(frozen=True)
class ToolPaths:
    toolkit: Path
    project: Path
    work: Path
    yt_dlp: Path
    aria2: Path
    ffmpeg: Path
    ffprobe: Path
    python: Path
    node: Path | None
    media_dependencies: Path
    whisper_models: Path
    offline_translation_model: Path
    transcripts: Path
    metadata: Path
    logs: Path
    build_subtitles_script: Path
    translate_captions_script: Path
    packaged: bool = False

    @classmethod
    def discover(cls, toolkit: Path | None = None, config: dict | None = None) -> "ToolPaths":
        toolkit = (toolkit or Path(__file__).resolve().parent).resolve()
        project = toolkit.parent
        configured_home = os.environ.get("VIDEO_SUBTITLE_TOOLKIT_HOME", "").strip()
        if configured_home:
            work = Path(configured_home).expanduser().resolve()
        else:
            local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
            base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
            work = (base / "VideoSubtitleToolkit").resolve()
        config = config or DEFAULT_CONFIG

        def first(root: Path, name: str) -> Path:
            matches = sorted(root.rglob(name)) if root.exists() else []
            return matches[0] if matches else root / name

        tools_root = work / "tools"
        ffmpeg_root = tools_root / "ffmpeg"
        aria_root = tools_root / "aria2"
        configured_python = str(config.get("python_path", "")).strip()
        python = Path(configured_python) if configured_python else Path(sys.executable)
        if python.name.lower() == "pythonw.exe":
            console_python = python.with_name("python.exe")
            if console_python.exists():
                python = console_python

        configured_node = str(config.get("node_path", "")).strip()
        node = Path(configured_node) if configured_node else None
        if node is None:
            local_node = first(tools_root / "node", "node.exe")
            command_node = shutil.which("node")
            if local_node.exists():
                node = local_node
            elif command_node:
                node = Path(command_node)

        return cls(
            toolkit=toolkit,
            project=project,
            work=work,
            yt_dlp=tools_root / "yt-dlp" / "yt-dlp.exe",
            aria2=first(aria_root, "aria2c.exe"),
            ffmpeg=first(ffmpeg_root, "ffmpeg.exe"),
            ffprobe=first(ffmpeg_root, "ffprobe.exe"),
            python=python,
            node=node,
            media_dependencies=work / "python-packages",
            whisper_models=work / "models" / "whisper",
            offline_translation_model=work / "models" / "opus-mt-en-zh",
            transcripts=work / "transcripts",
            metadata=work / "metadata",
            logs=work / "logs",
            build_subtitles_script=toolkit / "build_video_subtitles.py",
            translate_captions_script=toolkit / "translate_video_captions.py",
            packaged=bool(getattr(sys, "frozen", False)),
        )


@dataclass
class Diagnostic:
    level: str
    component: str
    message: str


@dataclass(frozen=True)
class ProgressSample:
    percent: float
    speed: str = ""
    eta: str = ""
    phase: str = ""


@dataclass(frozen=True)
class PerformanceLimits:
    download_default: int
    download_max: int
    translation_default: int
    translation_max: int


@dataclass
class VideoTask:
    video_id: str
    title: str
    webpage_url: str
    status: str = "等待"
    detail: str = ""
    progress: float = 0.0
    result: dict = field(default_factory=dict)


class CancelledError(RuntimeError):
    pass


def decode_output_line(raw: bytes) -> str:
    raw = raw.rstrip(b"\r\n")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode("gb18030")
        except UnicodeDecodeError:
            return raw.decode("utf-8", errors="replace")


def _friendly_eta(value: str) -> str:
    if re.fullmatch(r"\d+s", value):
        return f"{value[:-1]}秒"
    if re.fullmatch(r"\d+m\d*s?", value):
        return value.replace("m", "分").replace("s", "秒")
    return value


def parse_progress_line(line: str) -> ProgressSample | None:
    aria = re.search(
        r"\((\d+(?:\.\d+)?)%\).*?DL:([^\s\]]+)(?:\s+ETA:([^\s\]]+))?",
        line,
    )
    if aria:
        speed = aria.group(2)
        if speed != "0B" and not speed.endswith("/s"):
            speed += "/s"
        return ProgressSample(float(aria.group(1)), speed, _friendly_eta(aria.group(3) or ""), "下载")

    yt_dlp = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", line)
    if yt_dlp:
        speed = re.search(r"\sat\s+([^\s]+)", line)
        eta = re.search(r"\sETA\s+([^\s]+)", line)
        return ProgressSample(
            float(yt_dlp.group(1)),
            speed.group(1) if speed else "",
            _friendly_eta(eta.group(1) if eta else ""),
            "下载",
        )

    whisper = re.search(r"(\d+(?:\.\d+)?)%\|.*?\[[^<\]]*<([^,\]]+)", line)
    if whisper:
        return ProgressSample(float(whisper.group(1)), "", whisper.group(2).strip(), "英文转录")

    translation = re.search(r"(?:^|\s)[A-Za-z0-9_-]{11}:\s*(\d+)/(\d+)\s*$", line)
    if translation and int(translation.group(2)):
        return ProgressSample(
            int(translation.group(1)) / int(translation.group(2)) * 100,
            "",
            "",
            "中文字幕",
        )
    return None


def calculate_performance_limits(logical_cpus: int, memory_gb: float) -> PerformanceLimits:
    cpus = max(1, int(logical_cpus))
    download_max = min(6, max(1, cpus // 2))
    translation_max = min(4, max(1, cpus // 4))
    if memory_gb < 8:
        download_max = min(download_max, 2)
        translation_max = 1
    return PerformanceLimits(
        download_default=2 if download_max >= 2 else 1,
        download_max=download_max,
        translation_default=2 if translation_max >= 2 else 1,
        translation_max=translation_max,
    )


def detect_performance_limits() -> PerformanceLimits:
    memory_gb = 8.0
    if os.name == "nt":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            memory_gb = status.total_physical / (1024**3)
    return calculate_performance_limits(os.cpu_count() or 1, memory_gb)


def load_config(path: Path) -> tuple[dict, str | None]:
    config = DEFAULT_CONFIG.copy()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(loaded, dict):
            raise ValueError("配置根节点必须是 JSON 对象")
        config.update(loaded)
        return config, None
    except FileNotFoundError:
        return config, None
    except json.JSONDecodeError as exc:
        return config, f"配置文件格式错误：第 {exc.lineno} 行第 {exc.colno} 列"
    except (OSError, ValueError) as exc:
        return config, f"配置文件无法读取：{exc}"


def save_config(path: Path, config: dict) -> None:
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def video_id_from_url(url: str) -> str | None:
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower().split(":")[0]
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/")[0]
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if parsed.path == "/watch":
            candidate = parse_qs(parsed.query).get("v", [""])[0]
        elif parsed.path.startswith("/shorts/") or parsed.path.startswith("/embed/"):
            candidate = parsed.path.strip("/").split("/")[1]
        else:
            return None
    else:
        return None
    return candidate if VIDEO_ID_PATTERN.fullmatch(candidate) else None


def extract_urls(text: str) -> list[str]:
    candidates = re.findall(r"https?://[^\s<>\"]+", text)
    if not candidates:
        raise ValueError("请至少输入一个 YouTube 链接")
    results: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        url = raw.rstrip(".,;，。；）)")
        parsed = urlparse(url)
        host = parsed.netloc.lower().split(":")[0]
        if host not in SUPPORTED_HOSTS:
            raise ValueError(f"仅支持 YouTube 单视频或播放列表链接：{url}")
        video_id = video_id_from_url(url)
        key = f"video:{video_id}" if video_id else url
        if key not in seen:
            seen.add(key)
            results.append(url)
    return results


def parse_flat_playlist(payload: dict) -> list[VideoTask]:
    raw_entries = payload.get("entries")
    entries = raw_entries if isinstance(raw_entries, list) else [payload]
    tasks: list[VideoTask] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        video_id = str(entry.get("id") or "")
        if not VIDEO_ID_PATTERN.fullmatch(video_id) or video_id in seen:
            continue
        seen.add(video_id)
        webpage_url = entry.get("webpage_url") or entry.get("url") or ""
        if not str(webpage_url).startswith("http"):
            webpage_url = f"https://www.youtube.com/watch?v={video_id}"
        tasks.append(VideoTask(video_id, entry.get("title") or video_id, str(webpage_url)))
    return tasks


def _timestamp_seconds(value: str) -> float:
    hour, minute, rest = value.split(":")
    second, millisecond = rest.split(",")
    return int(hour) * 3600 + int(minute) * 60 + int(second) + int(millisecond) / 1000


def subtitle_summary(path: Path, duration: float, require_chinese: bool = False) -> tuple[int, str]:
    if not path.exists():
        return 0, "缺失"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return 0, "无法读取"
    cues = SRT_CUE_PATTERN.findall(text)
    sequence_numbers = re.findall(r"(?m)^(\d+)\s*$", text)
    if not cues or len(cues) != len(sequence_numbers):
        return len(cues), "格式无效"
    if [int(number) for number, _, _ in cues] != list(range(1, len(cues) + 1)):
        return len(cues), "序号无效"
    if require_chinese and not re.search(r"[\u4e00-\u9fff]", text):
        return len(cues), "无中文文本"
    starts: list[float] = []
    for _, start, end in cues:
        start_seconds = _timestamp_seconds(start)
        end_seconds = _timestamp_seconds(end)
        if start_seconds >= end_seconds:
            return len(cues), "时间范围无效"
        starts.append(start_seconds)
    if starts != sorted(starts):
        return len(cues), "时间顺序无效"
    if _timestamp_seconds(cues[-1][2]) > duration + 2:
        return len(cues), "超过视频时长"
    return len(cues), "OK"


def find_video_file(output_directory: Path, video_id: str) -> Path | None:
    suffix = f"[{video_id}].mkv"
    matches = sorted(path for path in output_directory.glob("*.mkv") if path.name.endswith(suffix))
    return matches[0] if len(matches) == 1 else None


def remap_project_path(path: Path, project_root: Path, mapped_root: Path) -> Path:
    try:
        relative = path.relative_to(project_root)
    except ValueError:
        return path
    return mapped_root / relative


@contextmanager
def ascii_project_drive(project_root: Path):
    if os.name != "nt" or str(project_root).isascii():
        yield project_root
        return
    selected: str | None = None
    for letter in "ZYXWVUT":
        drive = f"{letter}:"
        if Path(drive + "\\").exists():
            continue
        result = subprocess.run(
            ["subst", drive, str(project_root)],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode == 0:
            selected = drive
            break
    if selected is None:
        raise RuntimeError("无法创建离线翻译所需的临时短路径映射")
    try:
        yield Path(selected + "\\")
    finally:
        subprocess.run(
            ["subst", selected, "/D"],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )


def write_reports(report_directory: Path, tasks: Iterable[VideoTask]) -> None:
    report_directory.mkdir(parents=True, exist_ok=True)
    task_list = list(tasks)
    rows = [asdict(task) for task in task_list]
    (report_directory / "summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (report_directory / "summary.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["video_id", "title", "url", "status", "detail", "progress", "video_file"])
        for task in task_list:
            writer.writerow(
                [
                    task.video_id,
                    task.title,
                    task.webpage_url,
                    task.status,
                    task.detail,
                    task.progress,
                    task.result.get("video_file", ""),
                ]
            )
    errors = [f"{task.video_id}\t{task.title}\t{task.detail}" for task in task_list if task.status == "失败"]
    (report_directory / "errors.txt").write_text(
        ("\n".join(errors) + "\n") if errors else "本次运行没有失败项。\n",
        encoding="utf-8-sig",
    )


class ProcessRunner:
    def __init__(self, cancel_event: threading.Event | None = None):
        self.cancel_event = cancel_event or threading.Event()
        self.current: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def run(
        self,
        arguments: list[str],
        *,
        env: dict | None = None,
        cwd: Path | None = None,
        on_line: Callable[[str], None] | None = None,
    ) -> int:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(
            arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=False,
            env=env,
            cwd=str(cwd) if cwd else None,
            creationflags=flags,
        )
        with self._lock:
            self.current = process
        try:
            assert process.stdout is not None
            for raw_line in process.stdout:
                if self.cancel_event.is_set():
                    self.stop()
                    raise CancelledError("任务已停止")
                if on_line:
                    on_line(decode_output_line(raw_line))
            code = process.wait()
            if self.cancel_event.is_set():
                raise CancelledError("任务已停止")
            return code
        finally:
            if process.stdout is not None:
                process.stdout.close()
            with self._lock:
                self.current = None

    def capture_json(self, arguments: list[str], *, env: dict | None = None) -> dict:
        lines: list[str] = []
        code = self.run(arguments, env=env, on_line=lines.append)
        text = "\n".join(lines).strip()
        if code:
            raise RuntimeError(text or f"命令退出代码：{code}")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            start = text.find("{")
            if start >= 0:
                try:
                    return json.loads(text[start:])
                except json.JSONDecodeError:
                    pass
            raise RuntimeError(f"无法解析 yt-dlp 元数据：{exc}") from exc

    def stop(self) -> None:
        self.cancel_event.set()
        with self._lock:
            process = self.current
        if process and process.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                process.terminate()


def _command_version(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, "文件不存在"
    try:
        result = subprocess.run(
            [str(path), "-version" if path.name.lower().startswith("ff") else "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        first_line = (result.stdout or result.stderr).splitlines()[0]
        return result.returncode == 0, first_line[:160]
    except (OSError, subprocess.SubprocessError, IndexError) as exc:
        return False, str(exc)


def check_online_translation(timeout: int = 8) -> tuple[bool, str]:
    query = urlencode({"client": "gtx", "sl": "en", "tl": "zh-CN", "dt": "t", "q": "test"})
    try:
        with urllib.request.urlopen(
            f"https://translate.googleapis.com/translate_a/single?{query}", timeout=timeout
        ) as response:
            payload = json.load(response)
        translated = payload[0][0][0]
        if not isinstance(translated, str) or not translated.strip():
            raise ValueError("返回内容为空")
        return True, f"可用，测试结果：{translated}"
    except (OSError, ValueError, IndexError, TypeError, json.JSONDecodeError) as exc:
        return False, str(exc)


def diagnose_environment(paths: ToolPaths, config: dict, check_network: bool = False) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    if paths.packaged:
        diagnostics.append(Diagnostic("OK", "运行环境", "Windows 便携版内置运行时"))
    else:
        ok, message = _command_version(paths.python)
        diagnostics.append(Diagnostic("OK" if ok else "错误", "Python", message))
    required = {
        "yt-dlp": paths.yt_dlp,
        "aria2": paths.aria2,
        "FFmpeg": paths.ffmpeg,
        "FFprobe": paths.ffprobe,
        "转录脚本": paths.build_subtitles_script,
        "翻译脚本": paths.translate_captions_script,
    }
    for component, path in required.items():
        if component in {"转录脚本", "翻译脚本"}:
            ok, message = path.is_file(), str(path)
        else:
            ok, message = _command_version(path)
        diagnostics.append(Diagnostic("OK" if ok else "错误", component, message))

    model = paths.whisper_models / f"{config.get('whisper_model', 'small.en')}.pt"
    diagnostics.append(Diagnostic("OK" if model.is_file() else "错误", "Whisper 模型", str(model)))
    diagnostics.append(
        Diagnostic(
            "OK" if paths.media_dependencies.is_dir() else "错误",
            "Python 媒体依赖",
            str(paths.media_dependencies),
        )
    )
    if paths.node and paths.node.is_file():
        diagnostics.append(Diagnostic("OK", "Node.js", str(paths.node)))
    else:
        diagnostics.append(Diagnostic("警告", "Node.js", "未找到；部分 YouTube 视频解析可能受影响"))

    output = resolve_output_directory(paths, config)
    try:
        output.mkdir(parents=True, exist_ok=True)
        probe = output / ".write-test.tmp"
        probe.write_text("ok", encoding="ascii")
        probe.unlink()
        diagnostics.append(Diagnostic("OK", "成品目录", str(output)))
    except OSError as exc:
        diagnostics.append(Diagnostic("错误", "成品目录", str(exc)))

    try:
        free_gb = shutil.disk_usage(output).free / (1024**3)
        threshold = float(config.get("minimum_free_gb", 2))
        level = "OK" if free_gb >= threshold else "警告"
        diagnostics.append(Diagnostic(level, "磁盘空间", f"可用 {free_gb:.1f} GB，建议至少 {threshold:.1f} GB"))
    except OSError as exc:
        diagnostics.append(Diagnostic("警告", "磁盘空间", str(exc)))

    mode = config.get("translation_mode", "auto")
    if check_network and config.get("chinese_subtitles", True) and mode in {"auto", "online"}:
        ok, message = check_online_translation()
        if ok:
            diagnostics.append(Diagnostic("OK", "在线翻译", message))
        elif mode == "auto" and paths.offline_translation_model.is_dir():
            diagnostics.append(Diagnostic("警告", "在线翻译", f"{message}；自动模式将使用本地模型"))
        else:
            diagnostics.append(Diagnostic("警告", "在线翻译", f"当前无法连接：{message}"))
    if config.get("chinese_subtitles", True) and mode in {"auto", "offline"}:
        exists = paths.offline_translation_model.is_dir()
        diagnostics.append(
            Diagnostic("OK" if exists else "错误", "本地翻译模型", str(paths.offline_translation_model))
        )
    return diagnostics


def resolve_output_directory(paths: ToolPaths, config: dict) -> Path:
    configured = str(config.get("output_directory") or "").strip()
    if not configured:
        return (Path.home() / "Videos" / "VideoSubtitleToolkit").resolve()
    raw = Path(configured).expanduser()
    return raw.resolve() if raw.is_absolute() else (paths.work / raw).resolve()


class RunLogger:
    def __init__(self, path: Path, callback: Callable[[str], None] | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.callback = callback
        self._lock = threading.Lock()
        self._last_message = ""

    def __call__(self, message: str) -> None:
        message = message.strip("\r\n")
        if not message.strip():
            return
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}"
        with self._lock:
            if message == self._last_message:
                return
            self._last_message = message
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
        if self.callback:
            self.callback(line)


def _yt_dlp_common(paths: ToolPaths) -> list[str]:
    arguments = [
        str(paths.yt_dlp),
        "--ignore-config",
        "--socket-timeout",
        "15",
        "--extractor-retries",
        "2",
        "--retry-sleep",
        "2",
        "--encoding",
        "utf-8",
    ]
    if paths.node and paths.node.is_file():
        arguments.extend(["--js-runtimes", f"node:{paths.node}"])
    return arguments


def expand_urls(
    urls: Iterable[str],
    paths: ToolPaths,
    runner: ProcessRunner,
    log: Callable[[str], None],
) -> list[VideoTask]:
    tasks: list[VideoTask] = []
    seen: set[str] = set()
    for url in urls:
        log(f"解析链接：{url}")
        arguments = _yt_dlp_common(paths) + [
            "--quiet",
            "--no-warnings",
            "--flat-playlist",
            "--dump-single-json",
            "--skip-download",
            url,
        ]
        payload = runner.capture_json(arguments)
        for task in parse_flat_playlist(payload):
            if task.video_id not in seen:
                seen.add(task.video_id)
                tasks.append(task)
    if not tasks:
        raise RuntimeError("链接中没有找到可处理的公开视频")
    return tasks


class PipelineEngine:
    STAGES = ("元数据", "下载", "英文转录", "中文字幕", "校验")
    STAGE_WEIGHTS = {
        "metadata": 5.0,
        "download": 35.0,
        "transcribe": 35.0,
        "translate": 20.0,
        "validate": 5.0,
    }

    def __init__(
        self,
        paths: ToolPaths,
        config: dict,
        output_directory: Path,
        event_callback: Callable[[str, object], None] | None = None,
        cancel_event: threading.Event | None = None,
    ):
        self.paths = paths
        self.config = config
        self.output_directory = output_directory.resolve()
        self.event_callback = event_callback or (lambda _kind, _payload: None)
        self.cancel_event = cancel_event or threading.Event()
        self.runner = ProcessRunner(self.cancel_event)
        self._runners: set[ProcessRunner] = {self.runner}
        self._runner_lock = threading.Lock()
        self._task_lock = threading.RLock()
        self._stage_progress: dict[str, dict[str, float]] = {}
        self.performance_limits = detect_performance_limits()
        self.download_workers = max(
            1,
            min(int(config.get("download_workers", self.performance_limits.download_default)),
                self.performance_limits.download_max),
        )
        self.translation_workers = max(
            1,
            min(int(config.get("translation_workers", self.performance_limits.translation_default)),
                self.performance_limits.translation_max),
        )
        self.report_directory = paths.logs / datetime.now().strftime("%Y%m%d-%H%M%S")
        self.logger = RunLogger(self.report_directory / "run.log", lambda line: self._event("log", line))
        self.tasks: list[VideoTask] = []

    def _event(self, kind: str, payload: object) -> None:
        self.event_callback(kind, payload)

    def stop(self) -> None:
        self.logger("收到停止请求")
        self.cancel_event.set()
        with self._runner_lock:
            runners = list(self._runners)
        for runner in runners:
            runner.stop()

    @contextmanager
    def _job_runner(self):
        runner = ProcessRunner(self.cancel_event)
        with self._runner_lock:
            self._runners.add(runner)
        try:
            yield runner
        finally:
            with self._runner_lock:
                self._runners.discard(runner)

    def _set_status(self, task: VideoTask, status: str, detail: str = "") -> None:
        with self._task_lock:
            task.status = status
            task.detail = detail
            payload = asdict(task)
        self._event("task", payload)

    def _update_stage(
        self,
        task: VideoTask,
        stage: str,
        percent: float,
        *,
        status: str | None = None,
        detail: str | None = None,
    ) -> None:
        with self._task_lock:
            stages = self._stage_progress.setdefault(
                task.video_id, {name: 0.0 for name in self.STAGE_WEIGHTS}
            )
            stages[stage] = max(stages[stage], min(100.0, max(0.0, float(percent))))
            task.progress = round(
                sum(stages[name] * weight / 100 for name, weight in self.STAGE_WEIGHTS.items()), 1
            )
            if status is not None:
                task.status = status
            if detail is not None:
                task.detail = detail
            task_payload = asdict(task)
            all_progress = [item.progress for item in self.tasks] or [0.0]
            completed = sum(item.status == "完成" for item in self.tasks)
            overall = round(sum(all_progress) / len(all_progress), 1)
        self._event("task", task_payload)
        self._event(
            "progress",
            {"percent": overall, "completed": completed, "total": len(self.tasks)},
        )

    def _environment(self) -> dict:
        env = os.environ.copy()
        env["PATH"] = str(self.paths.ffmpeg.parent) + os.pathsep + env.get("PATH", "")
        current_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(self.paths.media_dependencies) + (
            os.pathsep + current_pythonpath if current_pythonpath else ""
        )
        env["PYTHONUTF8"] = "1"
        return env

    def _run_checked(
        self,
        arguments: list[str],
        task: VideoTask,
        stage: str,
        *,
        runner: ProcessRunner | None = None,
        progress_stage: str | None = None,
        env: dict | None = None,
    ) -> None:
        runner = runner or self.runner
        self.logger(f"{task.video_id} [{stage}] 开始")
        part = 0
        last_raw_percent = 0.0

        def on_line(line: str) -> None:
            nonlocal part, last_raw_percent
            self.logger(line)
            sample = parse_progress_line(line)
            if not sample or not progress_stage:
                return
            display_percent = sample.percent
            if progress_stage == "download":
                if last_raw_percent >= 95 and sample.percent < 25:
                    part = 1
                last_raw_percent = sample.percent
                display_percent = sample.percent / 2 if part == 0 else 50 + sample.percent / 2
            details = [stage, f"{sample.percent:.0f}%"]
            if sample.speed:
                details.append(sample.speed)
            if sample.eta:
                details.append(f"剩余 {sample.eta}")
            self._update_stage(
                task,
                progress_stage,
                display_percent,
                status=stage,
                detail=" · ".join(details),
            )

        code = runner.run(arguments, env=env, on_line=on_line)
        if code:
            raise RuntimeError(f"{stage}失败，命令退出代码 {code}")
        self.logger(f"{task.video_id} [{stage}] 完成")

    def _fetch_metadata(self, task: VideoTask, runner: ProcessRunner | None = None) -> tuple[Path, dict]:
        runner = runner or self.runner
        self.paths.metadata.mkdir(parents=True, exist_ok=True)
        metadata_path = self.paths.metadata / f"{task.video_id}.info.json"
        if metadata_path.exists():
            try:
                payload = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
                if payload.get("id") == task.video_id and payload.get("formats"):
                    self.logger(f"{task.video_id} 复用现有元数据")
                    task.title = payload.get("title") or task.title
                    return metadata_path, payload
            except (OSError, json.JSONDecodeError):
                pass
        arguments = _yt_dlp_common(self.paths) + [
            "--quiet",
            "--no-warnings",
            "--dump-single-json",
            "--skip-download",
            task.webpage_url,
        ]
        payload = runner.capture_json(arguments, env=self._environment())
        if payload.get("id") != task.video_id:
            raise RuntimeError("元数据视频 ID 与任务不一致")
        metadata_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        task.title = payload.get("title") or task.title
        return metadata_path, payload

    def _download(
        self, task: VideoTask, metadata_path: Path, runner: ProcessRunner | None = None
    ) -> Path:
        runner = runner or self.runner
        self.output_directory.mkdir(parents=True, exist_ok=True)
        existing = find_video_file(self.output_directory, task.video_id)
        if existing and existing.stat().st_size > 0:
            self.logger(f"{task.video_id} 复用现有视频：{existing.name}")
            return existing
        if not self.config.get("download_video", True):
            raise RuntimeError("未启用视频下载且成品目录中没有可复用视频")
        temp_directory = self.paths.work / "tmp" / "video-toolkit" / task.video_id
        temp_directory.mkdir(parents=True, exist_ok=True)
        output_template = "%(title).180B [%(id)s].%(ext)s"
        connections = max(2, 16 // self.download_workers)
        arguments = _yt_dlp_common(self.paths) + [
            "--load-info-json",
            str(metadata_path),
            "--continue",
            "--no-overwrites",
            "--windows-filenames",
            "--retries",
            str(self.config.get("download_retries", 10)),
            "--fragment-retries",
            str(self.config.get("download_retries", 10)),
            "--concurrent-fragments",
            str(connections),
            "--format",
            "bestvideo*+bestaudio/best",
            "--merge-output-format",
            "mkv",
            "--external-downloader",
            str(self.paths.aria2),
            "--external-downloader-args",
            f"aria2c:-x {connections} -s {connections} -k 1M --file-allocation=none",
            "--ffmpeg-location",
            str(self.paths.ffmpeg.parent),
            "--paths",
            f"home:{self.output_directory}",
            "--paths",
            f"temp:{temp_directory}",
            "--output",
            output_template,
            "--newline",
        ]
        self._run_checked(
            arguments,
            task,
            "下载",
            runner=runner,
            progress_stage="download",
            env=self._environment(),
        )
        downloaded = find_video_file(self.output_directory, task.video_id)
        if not downloaded:
            raise RuntimeError("下载命令完成，但未找到合并后的 MKV 文件")
        return downloaded

    def _transcribe(
        self, task: VideoTask, video: Path, runner: ProcessRunner | None = None
    ) -> None:
        if self.paths.packaged:
            raise RuntimeError(
                "轻量便携版未包含本地 AI 运行时。请取消英文/中文字幕，"
                "或按 README 使用源码版并安装 AI 可选依赖。"
            )
        self.paths.transcripts.mkdir(parents=True, exist_ok=True)
        model_name = str(self.config.get("whisper_model", "small.en"))
        transcript = self.paths.transcripts / f"{task.video_id}.{model_name}.json"
        english = video.with_name(video.stem + ".en.local.srt")
        if transcript.exists() and english.exists() and english.stat().st_size > 0:
            self.logger(f"{task.video_id} 复用现有英文转录")
            return
        arguments = [
            str(self.paths.python),
            str(self.paths.build_subtitles_script),
            str(video),
            "--model-dir",
            str(self.paths.whisper_models),
            "--translation-dir",
            str(self.paths.offline_translation_model),
            "--ffmpeg-dir",
            str(self.paths.ffmpeg.parent),
            "--work-dir",
            str(self.paths.transcripts),
            "--whisper-model",
            model_name,
        ]
        self._run_checked(
            arguments,
            task,
            "英文转录",
            runner=runner,
            progress_stage="transcribe",
            env=self._environment(),
        )

    def _has_chinese_subtitle(self, task: VideoTask, video: Path) -> bool:
        chinese = video.with_name(video.stem + ".zh-CN.srt")
        if chinese.exists() and chinese.stat().st_size > 0:
            self.logger(f"{task.video_id} 复用现有中文字幕")
            return True
        return False

    def _translate_online(
        self, task: VideoTask, video: Path, runner: ProcessRunner | None = None
    ) -> None:
        if self.paths.packaged:
            raise RuntimeError("轻量便携版不能启动外部 Python 翻译脚本，请使用源码版 AI 运行时。")
        if self._has_chinese_subtitle(task, video):
            return
        arguments = [
            str(self.paths.python),
            str(self.paths.translate_captions_script),
            str(video),
            "--work-dir",
            str(self.paths.transcripts),
        ]
        self._run_checked(
            arguments,
            task,
            "在线中文字幕",
            runner=runner,
            progress_stage="translate",
            env=self._environment(),
        )

    def _translate_offline(
        self, task: VideoTask, video: Path, runner: ProcessRunner | None = None
    ) -> None:
        if self.paths.packaged:
            raise RuntimeError("轻量便携版未包含离线翻译运行时，请使用源码版 AI 运行时。")
        if self._has_chinese_subtitle(task, video):
            return
        with ascii_project_drive(self.paths.project) as mapped_root:
            mapped = lambda path: remap_project_path(path, self.paths.project, mapped_root)
            arguments = [
                str(self.paths.python),
                str(mapped(self.paths.build_subtitles_script)),
                str(mapped(video)),
                "--model-dir",
                str(mapped(self.paths.whisper_models)),
                "--translation-dir",
                str(mapped(self.paths.offline_translation_model)),
                "--ffmpeg-dir",
                str(mapped(self.paths.ffmpeg.parent)),
                "--work-dir",
                str(mapped(self.paths.transcripts)),
                "--whisper-model",
                str(self.config.get("whisper_model", "small.en")),
                "--translate",
            ]
            self._run_checked(
                arguments,
                task,
                "本地离线翻译",
                runner=runner,
                progress_stage="translate",
                env=self._environment(),
            )

    def _validate(
        self,
        task: VideoTask,
        video: Path,
        metadata: dict,
        runner: ProcessRunner | None = None,
    ) -> dict:
        runner = runner or self.runner
        self._update_stage(task, "validate", 10, status="校验", detail="读取媒体信息")
        probe_arguments = [
            str(self.paths.ffprobe),
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(video),
        ]
        probe = runner.capture_json(probe_arguments)
        streams = probe.get("streams", [])
        video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
        audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
        if not video_stream or not audio_stream:
            raise RuntimeError("媒体文件缺少视频流或音频流")
        duration = float(probe.get("format", {}).get("duration") or 0)
        available_heights = [
            item.get("height") or 0
            for item in metadata.get("formats", [])
            if item.get("vcodec") not in (None, "none") and item.get("ext") != "mhtml"
        ]
        max_height = max(available_heights, default=int(video_stream.get("height") or 0))
        actual_height = int(video_stream.get("height") or 0)
        if max_height and actual_height != max_height:
            raise RuntimeError(f"当前视频为 {actual_height}p，源站最高可用画质为 {max_height}p")

        decode_arguments = [
            str(self.paths.ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "10" if duration > 12 else "0",
            "-i",
            str(video),
            "-t",
            "2",
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-f",
            "null",
            "NUL" if os.name == "nt" else "/dev/null",
        ]
        self._update_stage(task, "validate", 30, status="校验", detail="短时解码")
        self._run_checked(decode_arguments, task, "短时解码", runner=runner)

        english = video.with_name(video.stem + ".en.local.srt")
        chinese = video.with_name(video.stem + ".zh-CN.srt")
        en_count, en_status = subtitle_summary(english, duration)
        zh_count, zh_status = subtitle_summary(chinese, duration, require_chinese=True)
        if self.config.get("english_subtitles", True) and en_status != "OK":
            raise RuntimeError(f"英文字幕校验失败：{en_status}")
        if self.config.get("chinese_subtitles", True) and zh_status != "OK":
            raise RuntimeError(f"中文字幕校验失败：{zh_status}")

        self._update_stage(task, "validate", 65, status="校验", detail="计算 SHA256")

        digest = hashlib.sha256()
        with video.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                if self.cancel_event.is_set():
                    raise CancelledError("任务已停止")
                digest.update(chunk)
        return {
            "video_file": str(video),
            "width": int(video_stream.get("width") or 0),
            "height": actual_height,
            "source_max_height": max_height,
            "audio_codec": audio_stream.get("codec_name", ""),
            "duration_seconds": round(duration, 2),
            "size_bytes": video.stat().st_size,
            "sha256": digest.hexdigest(),
            "english_cues": en_count,
            "english_status": en_status,
            "chinese_cues": zh_count,
            "chinese_status": zh_status,
            "subtitle_origin": "Local Whisper transcription and machine translation; not official YouTube captions",
        }

    def process(self, tasks: list[VideoTask]) -> list[VideoTask]:
        self.tasks = tasks
        self._stage_progress = {
            task.video_id: {name: 0.0 for name in self.STAGE_WEIGHTS} for task in tasks
        }
        self.output_directory.mkdir(parents=True, exist_ok=True)
        self.paths.transcripts.mkdir(parents=True, exist_ok=True)
        self.paths.metadata.mkdir(parents=True, exist_ok=True)
        self.report_directory.mkdir(parents=True, exist_ok=True)
        self.logger(
            f"开始处理 {len(tasks)} 个视频；下载线程 {self.download_workers}，"
            f"在线翻译线程 {self.translation_workers}；输出目录：{self.output_directory}"
        )
        artifacts: dict[str, tuple[Path, dict]] = {}

        def download_job(task: VideoTask) -> tuple[Path, dict]:
            if self.cancel_event.is_set():
                raise CancelledError("任务已停止")
            with self._job_runner() as runner:
                self._update_stage(task, "metadata", 0, status="元数据", detail="读取视频信息")
                metadata_path, metadata = self._fetch_metadata(task, runner)
                self._update_stage(task, "metadata", 100, status="下载", detail="准备最高画质音视频")
                video = self._download(task, metadata_path, runner)
                self._update_stage(task, "download", 100, status="下载", detail="下载与合并完成")
                return video, metadata

        with ThreadPoolExecutor(max_workers=self.download_workers, thread_name_prefix="download") as pool:
            futures = {pool.submit(download_job, task): task for task in tasks}
            for future in as_completed(futures):
                task = futures[future]
                try:
                    artifacts[task.video_id] = future.result()
                except CancelledError:
                    self._set_status(task, "停止", "任务已停止")
                except Exception as exc:
                    self._set_status(task, "失败", str(exc))
                    self.logger(f"{task.video_id} 下载阶段失败：{exc}")
                write_reports(self.report_directory, self.tasks)

        needs_transcript = self.config.get("english_subtitles", True) or self.config.get(
            "chinese_subtitles", True
        )
        for task in tasks:
            if self.cancel_event.is_set():
                break
            artifact = artifacts.get(task.video_id)
            if not artifact or task.status == "失败":
                continue
            video, _metadata = artifact
            try:
                if needs_transcript:
                    self._update_stage(
                        task, "transcribe", 0, status="英文转录", detail="Whisper 本地识别"
                    )
                    self._transcribe(task, video, self.runner)
                self._update_stage(task, "transcribe", 100, status="英文转录", detail="英文字幕完成")
            except CancelledError:
                self._set_status(task, "停止", "任务已停止")
                break
            except Exception as exc:
                self._set_status(task, "失败", str(exc))
                self.logger(f"{task.video_id} 英文转录失败：{exc}")
            write_reports(self.report_directory, self.tasks)

        translation_candidates = [
            task
            for task in tasks
            if task.video_id in artifacts and task.status not in {"失败", "停止"}
        ]
        if not self.config.get("chinese_subtitles", True):
            for task in translation_candidates:
                self._update_stage(task, "translate", 100, status="中文字幕", detail="未启用")
        else:
            mode = self.config.get("translation_mode", "auto")
            offline_queue: list[VideoTask] = []
            use_online = mode in {"auto", "online"}
            if mode == "auto":
                online_ok, online_message = check_online_translation()
                if online_ok:
                    self.logger(f"在线翻译可用：{online_message}")
                else:
                    use_online = False
                    offline_queue.extend(translation_candidates)
                    self.logger(f"在线翻译不可用，全部切换本地模型：{online_message}")
            elif mode == "offline":
                offline_queue.extend(translation_candidates)

            if use_online and not self.cancel_event.is_set():
                def online_job(task: VideoTask) -> None:
                    if self.cancel_event.is_set():
                        raise CancelledError("任务已停止")
                    with self._job_runner() as runner:
                        video, _metadata = artifacts[task.video_id]
                        self._update_stage(
                            task, "translate", 0, status="中文字幕", detail="在线机器翻译"
                        )
                        self._translate_online(task, video, runner)
                        self._update_stage(
                            task, "translate", 100, status="中文字幕", detail="在线翻译完成"
                        )

                with ThreadPoolExecutor(
                    max_workers=self.translation_workers, thread_name_prefix="translation"
                ) as pool:
                    futures = {pool.submit(online_job, task): task for task in translation_candidates}
                    for future in as_completed(futures):
                        task = futures[future]
                        try:
                            future.result()
                        except CancelledError:
                            self._set_status(task, "停止", "任务已停止")
                        except Exception as exc:
                            if mode == "auto":
                                offline_queue.append(task)
                                self._set_status(task, "中文字幕", "在线失败，已切换本地模型")
                                self.logger(f"{task.video_id} 在线翻译失败，已切换本地模型：{exc}")
                            else:
                                self._set_status(task, "失败", str(exc))
                                self.logger(f"{task.video_id} 在线翻译失败：{exc}")
                        write_reports(self.report_directory, self.tasks)

            for task in offline_queue:
                if self.cancel_event.is_set() or task.status in {"失败", "停止"}:
                    break
                video, _metadata = artifacts[task.video_id]
                try:
                    self._update_stage(
                        task, "translate", 0, status="中文字幕", detail="本地离线翻译"
                    )
                    self._translate_offline(task, video, self.runner)
                    self._update_stage(
                        task, "translate", 100, status="中文字幕", detail="本地翻译完成"
                    )
                except CancelledError:
                    self._set_status(task, "停止", "任务已停止")
                    break
                except Exception as exc:
                    self._set_status(task, "失败", str(exc))
                    self.logger(f"{task.video_id} 本地翻译失败：{exc}")
                write_reports(self.report_directory, self.tasks)

        for task in tasks:
            if self.cancel_event.is_set():
                break
            artifact = artifacts.get(task.video_id)
            if not artifact or task.status in {"失败", "停止"}:
                continue
            video, metadata = artifact
            try:
                if self.config.get("validate_outputs", True):
                    task.result = self._validate(task, video, metadata, self.runner)
                else:
                    task.result = {"video_file": str(video)}
                self._update_stage(task, "validate", 100, status="完成", detail="处理完成")
            except CancelledError:
                self._set_status(task, "停止", "任务已停止")
                break
            except Exception as exc:
                self._set_status(task, "失败", str(exc))
                self.logger(f"{task.video_id} 校验失败：{exc}")
            write_reports(self.report_directory, self.tasks)

        if self.cancel_event.is_set():
            for task in tasks:
                if task.status not in {"完成", "失败", "停止"}:
                    self._set_status(task, "停止", "任务已停止，已保留可恢复结果")
        write_reports(self.report_directory, self.tasks)
        self._event("report", str(self.report_directory))
        return self.tasks


def format_diagnostics(diagnostics: Iterable[Diagnostic]) -> str:
    return "\n".join(f"[{item.level}] {item.component}：{item.message}" for item in diagnostics)


def main() -> int:
    parser = argparse.ArgumentParser(description="视频下载与字幕工具核心")
    parser.add_argument("--doctor", action="store_true", help="检查本地工具和模型")
    parser.add_argument("--network", action="store_true", help="自检时同时检查在线翻译连接")
    args = parser.parse_args()
    config, warning = load_config(Path(__file__).resolve().parent / "config.json")
    paths = ToolPaths.discover(config=config)
    if warning:
        print(f"[警告] 配置：{warning}")
    if args.doctor:
        diagnostics = diagnose_environment(paths, config, check_network=args.network)
        print(format_diagnostics(diagnostics))
        return 1 if any(item.level == "错误" for item in diagnostics) else 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
