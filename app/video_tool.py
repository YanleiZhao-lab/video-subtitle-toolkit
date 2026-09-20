from __future__ import annotations

import os
import queue
import shutil
import threading
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from dependency_manager import DependencyManager
from toolkit_core import (
    CancelledError,
    PipelineEngine,
    ToolPaths,
    diagnose_environment,
    detect_performance_limits,
    expand_urls,
    extract_urls,
    format_diagnostics,
    load_config,
    resolve_output_directory,
    save_config,
)


class VideoToolkitApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.toolkit_directory = Path(__file__).resolve().parent
        bootstrap_paths = ToolPaths.discover(self.toolkit_directory)
        self.config_path = bootstrap_paths.work / "config.json"
        self.config_data, self.config_warning = load_config(self.config_path)
        self.paths = ToolPaths.discover(self.toolkit_directory, self.config_data)
        self.dependency_manager = DependencyManager(
            self.toolkit_directory / "dependencies.json", self.paths.work
        )
        self.performance_limits = detect_performance_limits()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.engine: PipelineEngine | None = None
        self.worker: threading.Thread | None = None
        self.last_report_directory: Path | None = None
        self.task_rows: dict[str, str] = {}

        self.title("视频下载与字幕工具")
        self.geometry("1080x760")
        self.minsize(900, 640)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.icon_warning = ""
        try:
            self.iconbitmap(default=str(self.toolkit_directory / "assets" / "video-tool.ico"))
        except tk.TclError as exc:
            self.icon_warning = f"图标加载失败，不影响功能：{exc}"
        self._configure_style()
        self._build_ui()
        if self.icon_warning:
            self._append_log(self.icon_warning)
        self.after(100, self._poll_events)
        self.after(350, lambda: self.run_environment_check(show_dialog=False))

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Subtle.TLabel", foreground="#555555")
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Treeview", rowheight=26, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        self.option_add("*Font", ("Microsoft YaHei UI", 9))

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=3)
        self.rowconfigure(5, weight=2)

        header = ttk.Frame(self, padding=(16, 12, 16, 4))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="视频下载与字幕工具", style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            header,
            text="YouTube 单视频 / 播放列表 · 最高画质下载 · 本地转录 · 中英字幕 · 完整校验",
            style="Subtle.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(3, 0))
        ttk.Button(header, text="组件管理", command=self.open_dependency_manager).grid(
            row=0, column=1, rowspan=2, sticky="e", padx=(0, 8)
        )
        ttk.Button(header, text="环境检查", command=self.run_environment_check).grid(
            row=0, column=2, rowspan=2, sticky="e"
        )

        input_group = ttk.LabelFrame(self, text="1  输入链接", padding=10)
        input_group.grid(row=1, column=0, sticky="nsew", padx=16, pady=(8, 6))
        input_group.columnconfigure(0, weight=1)
        self.link_text = tk.Text(input_group, height=4, wrap="word", undo=True, relief="solid", borderwidth=1)
        self.link_text.grid(row=0, column=0, rowspan=2, sticky="nsew")
        ttk.Button(input_group, text="从剪贴板粘贴", command=self.paste_links).grid(
            row=0, column=1, padx=(8, 0), sticky="ew"
        )
        ttk.Button(input_group, text="清空", command=lambda: self.link_text.delete("1.0", "end")).grid(
            row=1, column=1, padx=(8, 0), pady=(6, 0), sticky="ew"
        )

        settings = ttk.LabelFrame(self, text="2  处理设置", padding=10)
        settings.grid(row=2, column=0, sticky="ew", padx=16, pady=6)
        settings.columnconfigure(6, weight=1)
        self.download_var = tk.BooleanVar(value=bool(self.config_data.get("download_video", True)))
        self.english_var = tk.BooleanVar(value=bool(self.config_data.get("english_subtitles", True)))
        self.chinese_var = tk.BooleanVar(value=bool(self.config_data.get("chinese_subtitles", True)))
        self.validate_var = tk.BooleanVar(value=bool(self.config_data.get("validate_outputs", True)))
        ttk.Checkbutton(settings, text="最高画质视频", variable=self.download_var).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(settings, text="英文字幕", variable=self.english_var).grid(row=0, column=1, padx=(12, 0))
        ttk.Checkbutton(settings, text="中文字幕", variable=self.chinese_var).grid(row=0, column=2, padx=(12, 0))
        ttk.Checkbutton(settings, text="完成后校验", variable=self.validate_var).grid(row=0, column=3, padx=(12, 0))
        ttk.Label(settings, text="翻译：").grid(row=0, column=4, padx=(18, 3))
        translation_labels = {
            "auto": "自动（在线优先，失败离线）",
            "online": "仅在线",
            "offline": "完全离线",
        }
        self.translation_display = tk.StringVar(
            value=translation_labels.get(
                self.config_data.get("translation_mode", "auto"), translation_labels["auto"]
            )
        )
        self.translation_combo = ttk.Combobox(
            settings,
            state="readonly",
            width=24,
            textvariable=self.translation_display,
            values=tuple(translation_labels.values()),
        )
        self.translation_combo.grid(row=0, column=5, sticky="w")

        download_value = max(
            1,
            min(
                int(self.config_data.get("download_workers", self.performance_limits.download_default)),
                self.performance_limits.download_max,
            ),
        )
        translation_value = max(
            1,
            min(
                int(self.config_data.get("translation_workers", self.performance_limits.translation_default)),
                self.performance_limits.translation_max,
            ),
        )
        self.download_workers_var = tk.IntVar(value=download_value)
        self.translation_workers_var = tk.IntVar(value=translation_value)
        ttk.Label(settings, text="同时下载：").grid(row=1, column=0, pady=(9, 0), sticky="w")
        self.download_workers_spin = ttk.Spinbox(
            settings,
            from_=1,
            to=self.performance_limits.download_max,
            width=5,
            textvariable=self.download_workers_var,
        )
        self.download_workers_spin.grid(row=1, column=1, pady=(9, 0), sticky="w")
        ttk.Label(
            settings,
            text=f"推荐 {self.performance_limits.download_default}，范围 1–{self.performance_limits.download_max}",
            style="Subtle.TLabel",
        ).grid(row=1, column=2, columnspan=2, pady=(9, 0), sticky="w")
        ttk.Label(settings, text="翻译线程：").grid(row=1, column=4, pady=(9, 0), sticky="e")
        self.translation_workers_spin = ttk.Spinbox(
            settings,
            from_=1,
            to=self.performance_limits.translation_max,
            width=5,
            textvariable=self.translation_workers_var,
        )
        self.translation_workers_spin.grid(row=1, column=5, pady=(9, 0), sticky="w")
        ttk.Label(
            settings,
            text=f"推荐 {self.performance_limits.translation_default}，范围 1–{self.performance_limits.translation_max}",
            style="Subtle.TLabel",
        ).grid(row=1, column=6, pady=(9, 0), sticky="w")

        ttk.Label(settings, text="成品目录：").grid(row=2, column=0, pady=(10, 0), sticky="w")
        self.output_var = tk.StringVar(value=str(resolve_output_directory(self.paths, self.config_data)))
        self.output_entry = ttk.Entry(settings, textvariable=self.output_var)
        self.output_entry.grid(row=2, column=1, columnspan=5, sticky="ew", padx=(4, 8), pady=(10, 0))
        ttk.Button(settings, text="选择…", command=self.choose_output_directory).grid(
            row=2, column=6, sticky="e", pady=(10, 0)
        )
        ttk.Label(
            settings,
            text="工具、模型、缓存与日志保存在用户应用数据目录，可通过环境检查查看。",
            style="Subtle.TLabel",
        ).grid(row=3, column=0, columnspan=7, sticky="w", pady=(7, 0))

        task_group = ttk.LabelFrame(self, text="3  任务", padding=8)
        task_group.grid(row=3, column=0, sticky="nsew", padx=16, pady=6)
        task_group.columnconfigure(0, weight=1)
        task_group.rowconfigure(0, weight=1)
        columns = ("id", "title", "status", "progress", "detail")
        self.task_tree = ttk.Treeview(task_group, columns=columns, show="headings", selectmode="browse")
        headings = {"id": "视频 ID", "title": "标题", "status": "阶段", "progress": "进度", "detail": "说明"}
        widths = {"id": 105, "title": 290, "status": 90, "progress": 65, "detail": 330}
        for column in columns:
            self.task_tree.heading(column, text=headings[column])
            self.task_tree.column(
                column,
                width=widths[column],
                minwidth=55,
                anchor="center" if column in {"id", "status", "progress"} else "w",
                stretch=column in {"title", "detail"},
            )
        task_scroll = ttk.Scrollbar(task_group, orient="vertical", command=self.task_tree.yview)
        self.task_tree.configure(yscrollcommand=task_scroll.set)
        self.task_tree.grid(row=0, column=0, sticky="nsew")
        task_scroll.grid(row=0, column=1, sticky="ns")

        progress_frame = ttk.Frame(self, padding=(16, 3))
        progress_frame.grid(row=4, column=0, sticky="ew")
        progress_frame.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_frame, mode="determinate", maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")
        self.progress_label = ttk.Label(progress_frame, text="就绪", width=22, anchor="e")
        self.progress_label.grid(row=0, column=1, padx=(8, 0))

        log_group = ttk.LabelFrame(self, text="运行日志", padding=8)
        log_group.grid(row=5, column=0, sticky="nsew", padx=16, pady=6)
        log_group.columnconfigure(0, weight=1)
        log_group.rowconfigure(0, weight=1)
        self.log_text = tk.Text(
            log_group,
            height=7,
            wrap="word",
            state="disabled",
            background="#FAFAFA",
            foreground="#202020",
            relief="solid",
            borderwidth=1,
        )
        log_scroll = ttk.Scrollbar(log_group, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll.grid(row=0, column=1, sticky="ns")

        controls = ttk.Frame(self, padding=(16, 6, 16, 14))
        controls.grid(row=6, column=0, sticky="ew")
        controls.columnconfigure(3, weight=1)
        self.start_button = ttk.Button(controls, text="开始处理", style="Primary.TButton", command=self.start_processing)
        self.start_button.grid(row=0, column=0, padx=(0, 8))
        self.stop_button = ttk.Button(controls, text="停止", command=self.stop_processing, state="disabled")
        self.stop_button.grid(row=0, column=1, padx=(0, 8))
        self.retry_button = ttk.Button(controls, text="重试失败项", command=self.start_processing)
        self.retry_button.grid(row=0, column=2)
        ttk.Button(controls, text="打开成品目录", command=self.open_output_directory).grid(row=0, column=4, padx=(8, 0))
        ttk.Button(controls, text="导出运行报告", command=self.export_report).grid(row=0, column=5, padx=(8, 0))

    def paste_links(self) -> None:
        try:
            value = self.clipboard_get()
        except tk.TclError:
            messagebox.showinfo("剪贴板", "剪贴板中没有可粘贴的文本。", parent=self)
            return
        current = self.link_text.get("1.0", "end").strip()
        self.link_text.insert("end", ("\n" if current else "") + value.strip())

    def choose_output_directory(self) -> None:
        directory = filedialog.askdirectory(initialdir=self.output_var.get(), parent=self)
        if directory:
            self.output_var.set(directory)

    def open_dependency_manager(self) -> None:
        window = tk.Toplevel(self)
        window.title("可选组件管理")
        window.geometry("760x430")
        window.transient(self)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(1, weight=1)
        ttk.Label(
            window,
            text="基础媒体工具和 AI 模型按需下载；所有文件均校验 SHA-256。",
            padding=(12, 12, 12, 6),
        ).grid(row=0, column=0, sticky="w")
        tree = ttk.Treeview(window, columns=("name", "state", "detail"), show="headings")
        tree.heading("name", text="组件")
        tree.heading("state", text="状态")
        tree.heading("detail", text="说明 / 安装位置")
        tree.column("name", width=180)
        tree.column("state", width=90, anchor="center")
        tree.column("detail", width=450)
        tree.grid(row=1, column=0, sticky="nsew", padx=12)
        progress = ttk.Progressbar(window, maximum=100)
        progress.grid(row=2, column=0, sticky="ew", padx=12, pady=(8, 2))
        status = ttk.Label(window, text="请选择组件。", padding=(12, 2))
        status.grid(row=3, column=0, sticky="w")
        buttons = ttk.Frame(window, padding=12)
        buttons.grid(row=4, column=0, sticky="ew")

        def refresh() -> None:
            tree.delete(*tree.get_children())
            for item in self.dependency_manager.states():
                tree.insert(
                    "", "end", iid=item.component_id,
                    values=(item.name, "已安装" if item.installed else "未安装", item.detail),
                )

        def selected() -> str | None:
            values = tree.selection()
            if not values:
                messagebox.showinfo("组件管理", "请先选择一个组件。", parent=window)
                return None
            return values[0]

        def install() -> None:
            component_id = selected()
            if not component_id:
                return
            install_button.configure(state="disabled")
            remove_button.configure(state="disabled")

            def report(done: int, total: int, label: str) -> None:
                percent = done * 100 / total if total else 0
                self.after(0, lambda: (progress.configure(value=percent), status.configure(
                    text=f"正在下载 {label}：{percent:.1f}%"
                )))

            def worker() -> None:
                try:
                    self.dependency_manager.install(component_id, report)
                    self.after(0, lambda: status.configure(text="安装完成。"))
                except Exception as exc:
                    message = str(exc)
                    self.after(0, lambda value=message: messagebox.showerror(
                        "安装失败", value, parent=window
                    ))
                finally:
                    self.after(0, lambda: (refresh(), install_button.configure(state="normal"),
                                            remove_button.configure(state="normal")))

            threading.Thread(target=worker, name="component-installer", daemon=True).start()

        def remove() -> None:
            component_id = selected()
            if not component_id:
                return
            if messagebox.askyesno("移除组件", "确定移除所选组件？", parent=window):
                try:
                    self.dependency_manager.remove(component_id)
                    progress.configure(value=0)
                    status.configure(text="组件已移除。")
                    refresh()
                except Exception as exc:
                    messagebox.showerror("移除失败", str(exc), parent=window)

        install_button = ttk.Button(buttons, text="安装 / 修复", command=install)
        install_button.pack(side="left")
        remove_button = ttk.Button(buttons, text="移除", command=remove)
        remove_button.pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="关闭", command=window.destroy).pack(side="right")
        refresh()

    def _collect_config(self) -> dict:
        config = self.config_data.copy()
        translation_modes = {
            "自动（在线优先，失败离线）": "auto",
            "仅在线": "online",
            "完全离线": "offline",
        }
        try:
            download_workers = int(self.download_workers_var.get())
        except (tk.TclError, ValueError):
            download_workers = self.performance_limits.download_default
        try:
            translation_workers = int(self.translation_workers_var.get())
        except (tk.TclError, ValueError):
            translation_workers = self.performance_limits.translation_default
        download_workers = max(1, min(download_workers, self.performance_limits.download_max))
        translation_workers = max(1, min(translation_workers, self.performance_limits.translation_max))
        self.download_workers_var.set(download_workers)
        self.translation_workers_var.set(translation_workers)
        config.update(
            {
                "output_directory": self.output_var.get().strip(),
                "download_video": self.download_var.get(),
                "english_subtitles": self.english_var.get(),
                "chinese_subtitles": self.chinese_var.get(),
                "validate_outputs": self.validate_var.get(),
                "translation_mode": translation_modes.get(self.translation_display.get(), "auto"),
                "download_workers": download_workers,
                "translation_workers": translation_workers,
            }
        )
        return config

    def start_processing(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("请稍候", "环境检查或其他后台任务仍在运行。", parent=self)
            return
        try:
            urls = extract_urls(self.link_text.get("1.0", "end"))
        except ValueError as exc:
            messagebox.showerror("链接无效", str(exc), parent=self)
            return
        config = self._collect_config()
        if config["chinese_subtitles"]:
            config["english_subtitles"] = True
            self.english_var.set(True)
        output = Path(config["output_directory"]).expanduser()
        if not output.is_absolute():
            output = (self.paths.work / output).resolve()
        config["output_directory"] = str(output)
        try:
            save_config(self.config_path, config)
        except OSError as exc:
            messagebox.showerror("无法保存配置", str(exc), parent=self)
            return
        self.config_data = config
        self.paths = ToolPaths.discover(self.toolkit_directory, config)
        diagnostics = diagnose_environment(self.paths, config, check_network=False)
        errors = [item for item in diagnostics if item.level == "错误"]
        if errors:
            messagebox.showerror("环境检查未通过", format_diagnostics(errors), parent=self)
            return
        self.cancel_event = threading.Event()
        self.engine = PipelineEngine(
            self.paths,
            config,
            output,
            event_callback=lambda kind, payload: self.events.put((kind, payload)),
            cancel_event=self.cancel_event,
        )
        self.progress["value"] = 0
        self.progress_label.configure(text="准备中")
        self._set_running(True)
        self._append_log("开始解析链接。")

        def worker() -> None:
            try:
                assert self.engine is not None
                tasks = expand_urls(urls, self.paths, self.engine.runner, self.engine.logger)
                self.events.put(("tasks", [task.__dict__.copy() for task in tasks]))
                self.engine.process(tasks)
                failed = sum(task.status == "失败" for task in tasks)
                stopped = sum(task.status == "停止" for task in tasks)
                self.events.put(
                    ("done", {"failed": failed, "stopped": stopped, "total": len(tasks)})
                )
            except CancelledError:
                self.events.put(("stopped", None))
            except Exception as exc:
                self.events.put(("fatal", f"{exc}\n\n{traceback.format_exc()}"))

        self.worker = threading.Thread(target=worker, name="video-toolkit-worker", daemon=True)
        self.worker.start()

    def stop_processing(self) -> None:
        if self.engine:
            self.progress_label.configure(text="正在停止")
            self.engine.stop()

    def run_environment_check(self, show_dialog: bool = True) -> None:
        if self.worker and self.worker.is_alive():
            return
        config = self._collect_config()

        def worker() -> None:
            diagnostics = diagnose_environment(self.paths, config, check_network=True)
            self.events.put(("diagnostics", {"items": diagnostics, "show_dialog": show_dialog}))

        self.worker = threading.Thread(target=worker, name="environment-check", daemon=True)
        self.worker.start()

    def _set_running(self, running: bool) -> None:
        self.start_button.configure(state="disabled" if running else "normal")
        self.retry_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        self.translation_combo.configure(state="disabled" if running else "readonly")
        self.download_workers_spin.configure(state="disabled" if running else "normal")
        self.translation_workers_spin.configure(state="disabled" if running else "normal")

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                self._apply_event(kind, payload)
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _apply_event(self, kind: str, payload: object) -> None:
        if kind == "log":
            self._append_log(str(payload))
        elif kind == "tasks":
            for item in payload:
                self._upsert_task(item)
        elif kind == "task":
            self._upsert_task(payload)
        elif kind == "progress":
            percent = float(payload.get("percent", 0))
            self.progress["value"] = percent
            self.progress_label.configure(
                text=f"{payload.get('completed', 0)}/{payload.get('total', 0)} · {percent:.1f}%"
            )
        elif kind == "report":
            self.last_report_directory = Path(str(payload))
        elif kind == "done":
            self._set_running(False)
            failed = payload["failed"]
            stopped = payload.get("stopped", 0)
            total = payload["total"]
            if stopped:
                self.progress_label.configure(text=f"已停止 · {stopped} 项未完成")
                self._append_log(
                    f"批次已停止：共 {total} 项，停止 {stopped} 项，失败 {failed} 项；已保留可恢复结果。"
                )
                messagebox.showinfo(
                    "任务已停止",
                    f"已停止 {stopped} 项任务，下载文件和处理进度均已保留。",
                    parent=self,
                )
            elif failed:
                self.progress_label.configure(text="部分失败")
                self._append_log(f"批次完成：共 {total} 项，失败 {failed} 项。")
                messagebox.showwarning("处理完成", f"共处理 {total} 项，其中 {failed} 项失败。请查看日志。", parent=self)
            else:
                self.progress_label.configure(text="已完成 · 100%")
                self._append_log(f"批次完成：{total} 项全部成功。")
                messagebox.showinfo("处理完成", f"{total} 项任务全部完成。", parent=self)
        elif kind == "fatal":
            self._set_running(False)
            self.progress_label.configure(text="失败")
            self._append_log(str(payload))
            messagebox.showerror("任务失败", str(payload).split("\n", 1)[0], parent=self)
        elif kind == "stopped":
            self._set_running(False)
            self.progress_label.configure(text="已停止")
            self._append_log("任务已由用户停止；已完成文件和进度缓存均已保留。")
        elif kind == "diagnostics":
            self.worker = None
            diagnostics = payload["items"]
            text = format_diagnostics(diagnostics)
            self._append_log("环境检查结果：\n" + text)
            if self.config_warning:
                text = self.config_warning + "\n\n" + text
            if payload["show_dialog"]:
                errors = [item for item in diagnostics if item.level == "错误"]
                if errors:
                    messagebox.showerror("环境检查", text, parent=self)
                else:
                    messagebox.showinfo("环境检查", text, parent=self)

    def _upsert_task(self, item: dict) -> None:
        video_id = item["video_id"]
        values = (
            video_id,
            item.get("title", ""),
            item.get("status", ""),
            f"{float(item.get('progress', 0)):.1f}%",
            item.get("detail", ""),
        )
        row = self.task_rows.get(video_id)
        if row and self.task_tree.exists(row):
            self.task_tree.item(row, values=values)
        else:
            self.task_rows[video_id] = self.task_tree.insert("", "end", values=values)

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def open_output_directory(self) -> None:
        path = Path(self.output_var.get()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def export_report(self) -> None:
        if not self.last_report_directory or not self.last_report_directory.exists():
            messagebox.showinfo("运行报告", "当前还没有可导出的运行报告。", parent=self)
            return
        target_parent = filedialog.askdirectory(title="选择报告导出位置", parent=self)
        if not target_parent:
            return
        target = Path(target_parent) / self.last_report_directory.name
        try:
            shutil.copytree(self.last_report_directory, target, dirs_exist_ok=True)
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc), parent=self)
            return
        messagebox.showinfo("导出完成", f"运行报告已保存到：\n{target}", parent=self)

    def on_close(self) -> None:
        if self.worker and self.worker.is_alive() and self.engine:
            if not messagebox.askyesno("任务仍在运行", "停止当前任务并关闭窗口？", parent=self):
                return
            self.engine.stop()
        self.destroy()


def main() -> None:
    app = VideoToolkitApp()
    app.mainloop()


if __name__ == "__main__":
    main()
