import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR / "app"))

from toolkit_core import (  # noqa: E402
    DEFAULT_CONFIG,
    PipelineEngine,
    ProcessRunner,
    ProgressSample,
    ToolPaths,
    VideoTask,
    calculate_performance_limits,
    decode_output_line,
    extract_urls,
    find_video_file,
    load_config,
    parse_flat_playlist,
    parse_progress_line,
    remap_project_path,
    subtitle_summary,
    video_id_from_url,
    write_reports,
)


class ToolkitCoreTests(unittest.TestCase):
    def make_paths(self, root):
        root = Path(root)
        work = root / "app-data"
        return ToolPaths(
            toolkit=root / "application",
            project=root,
            work=work,
            yt_dlp=work / "yt-dlp.exe",
            aria2=work / "aria2c.exe",
            ffmpeg=work / "ffmpeg.exe",
            ffprobe=work / "ffprobe.exe",
            python=Path(sys.executable),
            node=None,
            media_dependencies=work / "media",
            whisper_models=work / "models",
            offline_translation_model=work / "translation",
            transcripts=work / "transcripts",
            metadata=work / "metadata",
            logs=work / "logs",
            build_subtitles_script=work / "build.py",
            translate_captions_script=work / "translate.py",
        )

    def test_download_pool_runs_two_jobs_concurrently(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.make_paths(directory)
            output = Path(directory) / "output"
            barrier = threading.Barrier(2)
            state = {"active": 0, "maximum": 0}
            lock = threading.Lock()

            class FakeEngine(PipelineEngine):
                def _fetch_metadata(self, task, runner=None):
                    with lock:
                        state["active"] += 1
                        state["maximum"] = max(state["maximum"], state["active"])
                    barrier.wait(timeout=2)
                    with lock:
                        state["active"] -= 1
                    return paths.metadata / f"{task.video_id}.json", {"id": task.video_id, "formats": []}

                def _download(self, task, metadata_path, runner=None):
                    output.mkdir(parents=True, exist_ok=True)
                    video = output / f"{task.title} [{task.video_id}].mkv"
                    video.write_bytes(b"video")
                    return video

            config = DEFAULT_CONFIG.copy()
            config.update(
                {
                    "download_workers": 2,
                    "english_subtitles": False,
                    "chinese_subtitles": False,
                    "validate_outputs": False,
                }
            )
            engine = FakeEngine(paths, config, output)
            tasks = [
                VideoTask("tmDMAINN2LU", "A", "https://youtu.be/tmDMAINN2LU"),
                VideoTask("Yt1HxZeapgY", "B", "https://youtu.be/Yt1HxZeapgY"),
            ]
            engine.process(tasks)
            self.assertEqual(state["maximum"], 2)
            self.assertTrue(all(task.status == "完成" for task in tasks))

    def test_auto_translation_falls_back_to_local_once(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self.make_paths(directory)
            output = Path(directory) / "output"
            calls = {"online": 0, "offline": 0}

            class FakeEngine(PipelineEngine):
                def _fetch_metadata(self, task, runner=None):
                    return paths.metadata / f"{task.video_id}.json", {"id": task.video_id, "formats": []}

                def _download(self, task, metadata_path, runner=None):
                    output.mkdir(parents=True, exist_ok=True)
                    video = output / f"A [{task.video_id}].mkv"
                    video.write_bytes(b"video")
                    return video

                def _transcribe(self, task, video, runner=None):
                    return None

                def _translate_online(self, task, video, runner=None):
                    calls["online"] += 1
                    raise RuntimeError("simulated network failure")

                def _translate_offline(self, task, video, runner=None):
                    calls["offline"] += 1

            config = DEFAULT_CONFIG.copy()
            config.update(
                {
                    "download_workers": 1,
                    "translation_workers": 2,
                    "translation_mode": "auto",
                    "validate_outputs": False,
                }
            )
            engine = FakeEngine(paths, config, output)
            task = VideoTask("tmDMAINN2LU", "A", "https://youtu.be/tmDMAINN2LU")
            with patch("toolkit_core.check_online_translation", return_value=(True, "OK")):
                engine.process([task])
            self.assertEqual(calls, {"online": 1, "offline": 1})
            self.assertEqual(task.status, "完成")

    def test_decode_output_line_supports_utf8(self):
        self.assertEqual(decode_output_line("下载完成".encode("utf-8")), "下载完成")

    def test_decode_output_line_supports_gb18030(self):
        raw = "下载到：E:\\培训资料\\视频.mkv".encode("gb18030")
        self.assertEqual(decode_output_line(raw), "下载到：E:\\培训资料\\视频.mkv")

    def test_process_runner_decodes_gb18030_subprocess_output(self):
        lines = []
        command = (
            "import sys; "
            "sys.stdout.buffer.write('路径：E:/培训/视频.mkv\\n'.encode('gb18030')); "
            "sys.stdout.buffer.flush()"
        )
        code = ProcessRunner().run([sys.executable, "-c", command], on_line=lines.append)
        self.assertEqual(code, 0)
        self.assertEqual(lines, ["路径：E:/培训/视频.mkv"])

    def test_process_runner_stop_terminates_child(self):
        runner = ProcessRunner()
        ready = threading.Event()
        errors = []

        def run_child():
            try:
                runner.run(
                    [
                        sys.executable,
                        "-c",
                        "import time; print('ready', flush=True); time.sleep(30)",
                    ],
                    on_line=lambda line: ready.set() if line == "ready" else None,
                )
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=run_child)
        thread.start()
        self.assertTrue(ready.wait(timeout=5))
        runner.stop()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(any("停止" in str(exc) for exc in errors))

    def test_parse_aria2_progress(self):
        event = parse_progress_line("[#1f9e06 20MiB/36MiB(55%) CN:16 DL:2.2MiB ETA:7s]")
        self.assertEqual(event, ProgressSample(55.0, "2.2MiB/s", "7秒", "下载"))

    def test_parse_yt_dlp_progress(self):
        event = parse_progress_line("[download] 100% of 36.95MiB in 00:00:23 at 1.56MiB/s")
        self.assertEqual(event.percent, 100.0)
        self.assertEqual(event.speed, "1.56MiB/s")

    def test_parse_whisper_progress(self):
        event = parse_progress_line(" 32%|███▏ | 58162/183031 [02:55<05:20, 389.65frames/s]")
        self.assertEqual(event.percent, 32.0)
        self.assertEqual(event.phase, "英文转录")

    def test_parse_translation_progress(self):
        event = parse_progress_line("FirIbwTY8Ck: 492/554")
        self.assertAlmostEqual(event.percent, 88.8, places=1)
        self.assertEqual(event.phase, "中文字幕")

    def test_unrelated_line_has_no_progress(self):
        self.assertIsNone(parse_progress_line("[Merger] Merging formats into video.mkv"))

    def test_performance_limits_for_eight_cores_and_sixteen_gb(self):
        limits = calculate_performance_limits(logical_cpus=8, memory_gb=16)
        self.assertEqual(limits.download_max, 4)
        self.assertEqual(limits.download_default, 2)
        self.assertEqual(limits.translation_max, 2)
        self.assertEqual(limits.translation_default, 2)

    def test_low_memory_limits_concurrency(self):
        limits = calculate_performance_limits(logical_cpus=16, memory_gb=4)
        self.assertEqual(limits.download_max, 2)
        self.assertEqual(limits.translation_max, 1)

    def test_extract_urls_deduplicates_equivalent_video_links(self):
        text = (
            "https://youtu.be/tmDMAINN2LU\n"
            "https://www.youtube.com/watch?v=tmDMAINN2LU\n"
            "https://www.youtube.com/playlist?list=PL1234567890\n"
        )
        self.assertEqual(
            extract_urls(text),
            [
                "https://youtu.be/tmDMAINN2LU",
                "https://www.youtube.com/playlist?list=PL1234567890",
            ],
        )

    def test_extract_urls_rejects_unsupported_sites(self):
        with self.assertRaisesRegex(ValueError, "仅支持 YouTube"):
            extract_urls("https://example.com/video")

    def test_video_id_from_supported_urls(self):
        self.assertEqual(video_id_from_url("https://youtu.be/tmDMAINN2LU"), "tmDMAINN2LU")
        self.assertEqual(
            video_id_from_url("https://www.youtube.com/watch?v=tmDMAINN2LU&list=PL123"),
            "tmDMAINN2LU",
        )
        self.assertIsNone(video_id_from_url("https://www.youtube.com/playlist?list=PL123"))

    def test_load_config_merges_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"download_retries": 3}', encoding="utf-8")
            config, warning = load_config(path)
            self.assertIsNone(warning)
            self.assertEqual(config["download_retries"], 3)
            self.assertEqual(config["whisper_model"], DEFAULT_CONFIG["whisper_model"])

    def test_load_config_preserves_broken_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("{broken", encoding="utf-8")
            config, warning = load_config(path)
            self.assertEqual(config, DEFAULT_CONFIG)
            self.assertIn("配置文件格式错误", warning)
            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_parse_flat_playlist_deduplicates_entries(self):
        payload = {
            "entries": [
                {"id": "tmDMAINN2LU", "title": "A", "url": "https://youtu.be/tmDMAINN2LU"},
                {"id": "tmDMAINN2LU", "title": "A duplicate"},
                {"id": "Yt1HxZeapgY", "title": "B"},
            ]
        }
        tasks = parse_flat_playlist(payload)
        self.assertEqual([task.video_id for task in tasks], ["tmDMAINN2LU", "Yt1HxZeapgY"])
        self.assertEqual(tasks[1].webpage_url, "https://www.youtube.com/watch?v=Yt1HxZeapgY")

    def test_parse_single_video_payload(self):
        tasks = parse_flat_playlist({"id": "tmDMAINN2LU", "title": "Single"})
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].title, "Single")

    def test_subtitle_summary_accepts_ordered_chinese(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.zh-CN.srt"
            path.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n测试\n\n"
                "2\n00:00:01,100 --> 00:00:01,900\n完成\n\n",
                encoding="utf-8",
            )
            self.assertEqual(subtitle_summary(path, 2.0, require_chinese=True), (2, "OK"))

    def test_subtitle_summary_rejects_non_chinese_translation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.zh-CN.srt"
            path.write_text("1\n00:00:00,000 --> 00:00:01,000\nTest\n\n", encoding="utf-8")
            self.assertEqual(subtitle_summary(path, 2.0, require_chinese=True), (1, "无中文文本"))

    def test_find_video_file_requires_exact_video_id_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            good = output / "A [tmDMAINN2LU].mkv"
            good.write_bytes(b"video")
            (output / "other.mkv").write_bytes(b"other")
            self.assertEqual(find_video_file(output, "tmDMAINN2LU"), good)

    def test_remap_project_path_only_changes_descendants(self):
        source = Path("E:/project/source")
        mapped = Path("Z:/")
        self.assertEqual(
            remap_project_path(source / "models/model.bin", source, mapped),
            mapped / "models/model.bin",
        )
        outside = Path("D:/output/video.mkv")
        self.assertEqual(remap_project_path(outside, source, mapped), outside)

    def test_discover_uses_portable_application_data_root(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "portable-data"
            with patch.dict("os.environ", {"VIDEO_SUBTITLE_TOOLKIT_HOME": str(home)}):
                paths = ToolPaths.discover(Path(directory) / "application")
            self.assertEqual(paths.work, home.resolve())
            managed = (
                paths.yt_dlp, paths.aria2, paths.ffmpeg, paths.whisper_models,
                paths.offline_translation_model, paths.transcripts, paths.logs,
            )
            rendered = "\n".join(str(value) for value in managed)
            for forbidden in (".chatgpt", "Python312"):
                self.assertNotIn(forbidden, rendered)
            self.assertEqual(
                paths.yt_dlp,
                home.resolve() / "tools" / "yt-dlp" / "yt-dlp.exe",
            )

    def test_write_reports_outputs_json_csv_and_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            report_dir = Path(directory)
            tasks = [
                VideoTask("tmDMAINN2LU", "Title", "https://youtu.be/tmDMAINN2LU", "完成", "OK"),
                VideoTask("Yt1HxZeapgY", "Bad", "https://youtu.be/Yt1HxZeapgY", "失败", "network"),
            ]
            write_reports(report_dir, tasks)
            payload = json.loads((report_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(len(payload), 2)
            self.assertIn("Yt1HxZeapgY", (report_dir / "errors.txt").read_text(encoding="utf-8-sig"))
            self.assertTrue((report_dir / "summary.csv").exists())


if __name__ == "__main__":
    unittest.main()
