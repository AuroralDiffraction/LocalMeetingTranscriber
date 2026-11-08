"""
M4A Joiner + Meeting Transcriber
Combine split .m4a files into one file (optionally MP3), then optionally create a
meeting transcript (.txt) with speaker labels using WhisperX + pyannote, all locally.

  * Drag files (or a folder) onto the list, or use "Add files..."
  * Reorder with the Up/Down buttons, Ctrl+Up/Ctrl+Down, or by dragging a row
  * Output as M4A (joined without re-encoding: fast, no quality loss)
    or MP3 (re-encoded at the bitrate you pick, optionally mono)

Setup (once):
    pip install tkinterdnd2 imageio-ffmpeg
    pip install whisperx            (only needed for transcripts - see README.md)

ffmpeg on your PATH is used if present; otherwise the copy bundled with
imageio-ffmpeg is used. Without tkinterdnd2 the app still runs, but without
drag-and-drop from Explorer.
"""
import importlib.util
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import webbrowser
from collections import deque
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:
    HAS_DND = False

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import transcriber as tr  # noqa: E402  (light import: heavy ML libraries load only in the subprocess)

AUDIO_EXTS = {".m4a", ".m4b", ".mp4", ".aac", ".mp3", ".wav", ".flac", ".ogg", ".opus"}
MP3_BITRATES = ["64k", "96k", "128k", "160k", "192k", "256k", "320k"]
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
CONFIG_FILE = Path.home() / ".m4a_joiner.json"
LANGUAGES = ["auto-detect", "en  English", "zh  Chinese", "fr  French", "de  German", "es  Spanish",
             "it  Italian", "pt  Portuguese", "nl  Dutch", "ja  Japanese", "ko  Korean", "ru  Russian",
             "ar  Arabic", "hi  Hindi", "pl  Polish", "tr  Turkish", "sv  Swedish"]
SPEAKER_CHOICES = ["auto"] + [str(i) for i in range(1, 21)]


# --------------------------------------------------------------------------- ffmpeg helpers
def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def natural_key(path):
    """Sort 'part2' before 'part10'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", Path(path).name)]


def probe(ffmpeg, path):
    """Return (duration_seconds or None, audio signature like 'aac|44100|stereo')."""
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True,
                       text=True, errors="replace", creationflags=NO_WINDOW)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
    dur = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else None
    a = re.search(r"Audio:\s*(\w+).*?(\d+) Hz,\s*([^,]+)", r.stderr)
    return dur, ("|".join(a.groups()) if a else None)


def probe_duration(ffmpeg, path):
    return probe(ffmpeg, path)[0]


def fmt_time(sec):
    if sec is None:
        return "?"
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def build_command(ffmpeg, files, out, fmt, bitrate="128k", mono=False, copy=True, listfile=None):
    """Return the ffmpeg command list for joining `files` into `out`."""
    base = [ffmpeg, "-y", "-hide_banner", "-nostats", "-progress", "pipe:1"]
    if fmt == "m4a" and copy:
        # Concat demuxer + stream copy: no re-encoding, so no quality loss.
        with open(listfile, "w", encoding="utf-8") as f:
            for p in files:
                safe = Path(p).resolve().as_posix().replace("'", "'\\''")
                f.write(f"file '{safe}'\n")
        return base + ["-f", "concat", "-safe", "0", "-i", listfile,
                       "-map", "0:a", "-c", "copy", "-movflags", "+faststart", str(out)]

    # Re-encode path: concat filter copes with files whose formats differ.
    cmd = list(base)
    for p in files:
        cmd += ["-i", str(p)]
    streams = "".join(f"[{i}:a:0]" for i in range(len(files)))
    cmd += ["-filter_complex", f"{streams}concat=n={len(files)}:v=0:a=1[out]", "-map", "[out]"]
    if mono:
        cmd += ["-ac", "1"]
    if fmt == "mp3":
        cmd += ["-c:a", "libmp3lame", "-b:a", bitrate]
    else:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
    return cmd + [str(out)]


def run_ffmpeg(cmd, total_dur, on_progress, on_start=None):
    """Run ffmpeg, reporting progress (0..1). Returns (returncode, stderr text)."""
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL,
                                text=True, errors="replace", creationflags=NO_WINDOW)
        if on_start:
            on_start(proc)
        for line in proc.stdout:
            m = re.match(r"out_time_(?:us|ms)=(\d+)", line)
            if m and total_dur:
                on_progress(min(int(m[1]) / 1e6 / total_dur, 1.0))
        proc.wait()
        err.seek(0)
        return proc.returncode, err.read()


def join_files(ffmpeg, files, out, fmt, bitrate, mono, total_dur, on_progress, on_status, on_start=None):
    """Do the whole job. Returns (ok, error_text)."""
    with tempfile.TemporaryDirectory() as tmp:
        listfile = os.path.join(tmp, "list.txt")
        # Lossless joining only works when every file has the same codec/sample rate/channels.
        same_format = len({probe(ffmpeg, f)[1] for f in files}) == 1
        if fmt == "m4a" and not mono and same_format:
            on_status("Joining (no re-encode)...")
            code, log = run_ffmpeg(build_command(ffmpeg, files, out, "m4a", copy=True, listfile=listfile),
                                   total_dur, on_progress, on_start)
            if code == 0:
                return True, ""
            on_status("Direct join failed (files differ?) - re-encoding to AAC...")
        elif fmt == "m4a" and not mono:
            on_status("Files have different formats - re-encoding to AAC...")
        else:
            on_status("Encoding MP3..." if fmt == "mp3" else "Encoding M4A...")
        code, log = run_ffmpeg(build_command(ffmpeg, files, out, fmt, bitrate, mono, copy=False),
                               total_dur, on_progress, on_start)
        return code == 0, log




# --------------------------------------------------------------------------- config
def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg):
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


def unique_path(path):
    """meeting.txt -> meeting (2).txt if it already exists, so earlier transcripts are never overwritten."""
    path, n = Path(path), 2
    candidate = path
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    return candidate


def open_file(path):
    if sys.platform == "win32":
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# --------------------------------------------------------------------------- speaker naming dialog
class SpeakerDialog(tk.Toplevel):
    """Shows each detected speaker with talk time and sample quotes so the user can name them."""

    def __init__(self, parent, data):
        super().__init__(parent)
        self.title("Who is who?")
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        self.entries = {}

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Name the speakers (leave as-is to keep generic labels).\n"
                              "Give two speakers the same name to merge them.",
                  foreground="#555").pack(anchor="w", pady=(0, 8))

        stats = tr.speaker_stats(data)
        total = sum(t for t, _ in stats.values()) or 1
        for i, (label, name) in enumerate(tr.default_names(data).items()):
            talk, samples = stats.get(label, (0, []))
            box = ttk.LabelFrame(frame, text=f" {name}  -  speaks {tr.fmt_dur(talk)} ({talk / total:.0%}) ",
                                 padding=6)
            box.pack(fill="x", pady=4)
            e = ttk.Entry(box, width=40)
            e.insert(0, name)
            e.pack(anchor="w")
            self.entries[label] = e
            for s in samples[:2]:
                text = s["text"] if len(s["text"]) <= 150 else s["text"][:147] + "..."
                ttk.Label(box, text=f"[{tr.fmt_ts(s['start'])}]  “{text}”", wraplength=520,
                          foreground="#444").pack(anchor="w", pady=(3, 0))
            if i == 0:
                e.focus_set()
                e.select_range(0, "end")

        btns = ttk.Frame(frame)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Save names", command=self.ok).pack(side="right")
        ttk.Button(btns, text="Keep generic names", command=self.destroy).pack(side="right", padx=6)
        self.bind("<Return>", lambda e: self.ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    def ok(self):
        self.result = {label: (e.get().strip() or label) for label, e in self.entries.items()}
        self.destroy()


# --------------------------------------------------------------------------- settings window
class SettingsDialog(tk.Toplevel):
    """Hugging Face token + overview of downloaded models with 'open location' buttons."""

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("Settings")
        self.transient(app.root)
        self.minsize(660, 560)
        self.items = {}          # tree item id -> inventory dict
        self._inventory = None   # filled by the background scan
        self._token_result = None

        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)

        # ---- Hugging Face token
        tkf = ttk.LabelFrame(f, text="Hugging Face token (needed for speaker identification)", padding=8)
        tkf.pack(fill="x")
        tkf.columnconfigure(0, weight=1)
        self.entry = ttk.Entry(tkf, textvariable=app.token, show="•")
        self.entry.grid(row=0, column=0, columnspan=2, sticky="we")
        self.show = tk.BooleanVar(value=False)
        ttk.Checkbutton(tkf, text="Show", variable=self.show,
                        command=lambda: self.entry.configure(show="" if self.show.get() else "•")
                        ).grid(row=0, column=2, padx=(8, 0))
        ttk.Checkbutton(tkf, text="Remember on this computer", variable=app.remember_token
                        ).grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.check_btn = ttk.Button(tkf, text="Check access", command=self.check_token)
        self.check_btn.grid(row=1, column=1, columnspan=2, sticky="e", pady=(6, 0))
        self.token_status = ttk.Label(tkf, text="", foreground="#555", wraplength=600, justify="left")
        self.token_status.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        links = ttk.Frame(tkf)
        links.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
        for text, url in (("1. Accept the pyannote model terms", tr.TERMS_URL),
                          ("2. Create a 'Read' token", tr.TOKEN_URL)):
            lab = ttk.Label(links, text=text, foreground="#06c", cursor="hand2")
            lab.pack(side="left", padx=(0, 18))
            lab.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))

        # ---- models
        mf = ttk.LabelFrame(f, text="Models", padding=8)
        mf.pack(fill="both", expand=True, pady=(10, 0))
        top = ttk.Frame(mf)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text="Hugging Face cache:", foreground="#555").pack(side="left")
        ttk.Label(top, text=str(tr.hf_cache_dir())).pack(side="left", padx=(6, 0))

        body = ttk.Frame(mf)
        body.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(body, columns=("status", "size"), show="tree headings", selectmode="browse",
                                 height=12)
        self.tree.heading("#0", text="Model")
        self.tree.heading("status", text="Status")
        self.tree.heading("size", text="Size")
        self.tree.column("#0", width=330)
        self.tree.column("status", width=150)
        self.tree.column("size", width=90, anchor="e")
        self.tree.tag_configure("ok", foreground="#070")
        self.tree.tag_configure("missing", foreground="#888")
        self.tree.tag_configure("group", font=("TkDefaultFont", 9, "bold"))
        sb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.update_buttons())
        self.tree.bind("<Double-1>", lambda e: self.open_selected())

        btns = ttk.Frame(mf)
        btns.pack(fill="x", pady=(6, 0))
        self.open_btn = ttk.Button(btns, text="Open location", command=self.open_selected, state="disabled")
        self.open_btn.pack(side="left")
        ttk.Button(btns, text="Open cache folder", command=self.open_cache).pack(side="left", padx=6)
        ttk.Button(btns, text="Refresh", command=self.refresh).pack(side="left")
        self.model_status = ttk.Label(btns, text="", foreground="#555")
        self.model_status.pack(side="left", padx=(12, 0))
        ttk.Label(mf, text="Models download automatically the first time they are needed.",
                  foreground="#777").pack(anchor="w", pady=(6, 0))

        ttk.Button(f, text="Close", command=self.close).pack(anchor="e", pady=(10, 0))
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda e: self.close())
        self.refresh()

    # ---- token
    def check_token(self):
        token = self.app.token.get().strip()
        if not token:
            self.set_token_status("Paste a token first.", "#b60")
            return
        self.check_btn.configure(state="disabled")
        self.set_token_status("Checking with huggingface.co...", "#555")
        self._token_result = None
        threading.Thread(target=self._check_token_worker, args=(token,), daemon=True).start()
        self.after(150, self._poll_token)

    def _check_token_worker(self, token):
        try:
            from huggingface_hub import HfApi, auth_check
            from huggingface_hub.errors import GatedRepoError, HfHubHTTPError
        except ImportError:
            self._token_result = ("huggingface_hub is not installed in this Python (pip install whisperx).", "#b00")
            return
        try:
            user = HfApi().whoami(token=token).get("name", "?")
        except HfHubHTTPError as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            self._token_result = (("This token is not valid. Create a new 'Read' token and paste it here."
                                   if code in (401, 403) else f"Hugging Face returned an error: {e}"), "#b00")
            return
        except Exception as e:
            self._token_result = (f"Could not reach huggingface.co ({type(e).__name__}). "
                                  "Check your internet connection.", "#b00")
            return
        try:
            auth_check(tr.DIARIZATION_MODEL, token=token)
            self._token_result = (f"✓ Token works (account: {user}) and has access to the pyannote model.",
                                  "#070")
        except GatedRepoError:
            self._token_result = (f"Token works (account: {user}), but this account hasn't accepted the "
                                  f"pyannote model terms yet. Click '1. Accept the pyannote model terms' "
                                  f"(while logged in as {user}), then check again.", "#b60")
        except Exception as e:
            self._token_result = (f"Token works (account: {user}), but the model check failed: {e}", "#b60")

    def _poll_token(self):
        if not self.winfo_exists():
            return
        if self._token_result is None:
            self.after(150, self._poll_token)
            return
        self.set_token_status(*self._token_result)
        self.check_btn.configure(state="normal")

    def set_token_status(self, text, color):
        self.token_status.configure(text=text, foreground=color)

    # ---- models
    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        self.model_status.configure(text="Checking which models are downloaded...")
        self.update_buttons()
        self._inventory = None
        langs = [l.split()[0] for l in LANGUAGES if l != "auto-detect"]
        threading.Thread(target=lambda: setattr(self, "_inventory", tr.model_inventory(langs)), daemon=True).start()
        self.after(100, self._poll_inventory)

    def _poll_inventory(self):
        if not self.winfo_exists():
            return
        if self._inventory is None:
            self.after(100, self._poll_inventory)
            return
        self.show_inventory(self._inventory)

    def show_inventory(self, inventory):
        lang_names = {l.split()[0]: l for l in LANGUAGES}
        groups, done, total_size = {}, 0, 0
        for it in inventory:
            g = it["group"]
            if g not in groups:
                groups[g] = [self.tree.insert("", "end", text=g, open=True, tags=("group",)), 0, 0]
            name = lang_names.get(it["name"], it["name"])
            if it["group"].startswith("Speech") and it["name"] == self.app.model.get():
                name += "   (selected)"
            iid = self.tree.insert(groups[g][0], "end", text=name,
                                   values=("✓ Downloaded" if it["downloaded"] else "Not downloaded",
                                           fmt_size(it["size"]) if it["downloaded"] else ""),
                                   tags=("ok" if it["downloaded"] else "missing",))
            self.items[iid] = it
            groups[g][1] += it["downloaded"]
            groups[g][2] += 1
            if it["downloaded"]:
                done += 1
                total_size += it["size"]
        for g, (gid, n_ok, n) in groups.items():
            self.tree.item(gid, values=(f"{n_ok} of {n}", ""))
        self.model_status.configure(text=f"{done} model(s) downloaded, {fmt_size(total_size)} on disk")
        self.update_buttons()

    def selected_item(self):
        sel = self.tree.selection()
        return self.items.get(sel[0]) if sel else None

    def update_buttons(self):
        it = self.selected_item()
        self.open_btn.configure(state="normal" if it and it["downloaded"] else "disabled")

    def open_selected(self):
        it = self.selected_item()
        if not it or not it["downloaded"]:
            return
        path = Path(it["path"])
        if path.is_file():
            App.open_folder(path)   # opens Explorer with the file selected
        elif path.exists():
            open_file(path)
        else:
            messagebox.showinfo("Not found", f"This folder no longer exists:\n{path}", parent=self)

    def open_cache(self):
        cache = tr.hf_cache_dir()
        if cache.exists():
            open_file(cache)
        else:
            messagebox.showinfo("No cache yet", f"Nothing has been downloaded yet.\n\nModels will be saved in:\n{cache}",
                                parent=self)

    def close(self):
        self.app.remember_settings()
        self.app.update_controls()
        self.destroy()


# --------------------------------------------------------------------------- GUI
class App:
    def __init__(self, root):
        self.root = root
        self.ffmpeg = find_ffmpeg()
        self.cfg = load_config()
        self.paths = {}      # tree item id -> file path
        self.durations = {}  # tree item id -> seconds (or None)
        self.q = queue.Queue()
        self.busy = False
        self.proc = None     # running ffmpeg / transcriber process (for Cancel)
        self.cancelled = False
        self._drag_item = None

        root.title("M4A Joiner + Meeting Transcriber")
        root.geometry("820x700")
        root.minsize(700, 620)

        main = ttk.Frame(root, padding=10)
        main.pack(fill="both", expand=True)

        hint = "Drag & drop audio files or a folder here" if HAS_DND else \
            "Use 'Add files...'  (pip install tkinterdnd2 to enable drag & drop)"
        ttk.Label(main, text=hint, foreground="#555").pack(anchor="w", pady=(0, 4))

        # ---- file list + side buttons
        mid = ttk.Frame(main)
        mid.pack(fill="both", expand=True)

        cols = ("n", "name", "dur", "size")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended", height=8)
        for c, text, w, anchor, stretch in [("n", "#", 40, "e", False), ("name", "File", 380, "w", True),
                                            ("dur", "Duration", 80, "e", False), ("size", "Size", 80, "e", False)]:
            self.tree.heading(c, text=text)
            self.tree.column(c, width=w, anchor=anchor, stretch=stretch)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")

        side = ttk.Frame(mid, padding=(8, 0, 0, 0))
        side.pack(side="left", fill="y")
        self.side_buttons = []
        for text, cmd in [("Add files...", self.add_dialog), ("Remove", self.remove_selected),
                          ("Clear all", self.clear), (None, None),
                          ("Move up  ↑", lambda: self.move(-1)), ("Move down ↓", lambda: self.move(1)),
                          ("Sort by name", self.sort_by_name)]:
            if text is None:
                ttk.Separator(side).pack(fill="x", pady=6)
                continue
            b = ttk.Button(side, text=text, command=cmd, width=14)
            b.pack(fill="x", pady=2)
            self.side_buttons.append(b)

        # ---- audio output options
        opts = ttk.LabelFrame(main, text="Audio output", padding=8)
        opts.pack(fill="x", pady=(10, 0))
        self.fmt = tk.StringVar(value=self.cfg.get("format", "m4a"))
        self.bitrate = tk.StringVar(value=self.cfg.get("bitrate", "128k"))
        self.mono = tk.BooleanVar(value=self.cfg.get("mono", False))

        ttk.Radiobutton(opts, text="M4A  - join only, no re-encoding (fast, original quality)",
                        variable=self.fmt, value="m4a", command=self.update_summary).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Radiobutton(opts, text="MP3  - compress", variable=self.fmt, value="mp3",
                        command=self.update_summary).grid(row=1, column=0, sticky="w")
        ttk.Label(opts, text="Bitrate:").grid(row=1, column=1, padx=(16, 4))
        self.br_box = ttk.Combobox(opts, textvariable=self.bitrate, values=MP3_BITRATES, width=6, state="readonly")
        self.br_box.grid(row=1, column=2)
        self.br_box.bind("<<ComboboxSelected>>", lambda e: self.update_summary())
        ttk.Checkbutton(opts, text="Mono (sounds better at low bitrates; fine for speech)", variable=self.mono,
                        command=self.update_summary).grid(row=1, column=3, padx=(16, 0), sticky="w")
        self.summary = ttk.Label(opts, text="", foreground="#555")
        self.summary.grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # ---- transcript options
        tf = ttk.LabelFrame(main, text="Meeting transcript (WhisperX + pyannote, runs locally)", padding=8)
        tf.pack(fill="x", pady=(10, 0))
        self.diarize = tk.BooleanVar(value=self.cfg.get("diarize", True))
        self.model = tk.StringVar(value=self.cfg.get("model", "large-v3-turbo"))
        self.language = tk.StringVar(value=self.cfg.get("language", "auto-detect"))
        self.min_spk = tk.StringVar(value=self.cfg.get("min_speakers", "auto"))
        self.max_spk = tk.StringVar(value=self.cfg.get("max_speakers", "auto"))
        self.token = tk.StringVar(value=self.cfg.get("hf_token", "") or os.environ.get("HF_TOKEN", ""))
        self.remember_token = tk.BooleanVar(value=bool(self.cfg.get("hf_token")))

        row0 = ttk.Frame(tf)
        row0.grid(row=0, column=0, columnspan=6, sticky="we")
        ttk.Checkbutton(row0, text="Identify speakers", variable=self.diarize,
                        command=self.update_controls).pack(side="left")
        self.token_label = ttk.Label(row0, text="")
        self.token_label.pack(side="left", padx=(16, 0))
        ttk.Button(row0, text="Settings...", command=self.open_settings).pack(side="right")
        tf.columnconfigure(5, weight=1)

        ttk.Label(tf, text="Model:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(tf, textvariable=self.model, values=tr.MODELS, width=15, state="readonly"
                     ).grid(row=1, column=1, sticky="w", pady=(6, 0))
        ttk.Label(tf, text="Language:").grid(row=1, column=2, sticky="e", padx=(16, 4), pady=(6, 0))
        ttk.Combobox(tf, textvariable=self.language, values=LANGUAGES, width=14
                     ).grid(row=1, column=3, sticky="w", pady=(6, 0))
        spk = ttk.Frame(tf)
        spk.grid(row=1, column=4, columnspan=2, sticky="w", padx=(16, 0), pady=(6, 0))
        ttk.Label(spk, text="Speakers  min").pack(side="left")
        self.min_box = ttk.Spinbox(spk, values=SPEAKER_CHOICES, textvariable=self.min_spk, width=5, state="readonly")
        self.min_box.pack(side="left", padx=4)
        ttk.Label(spk, text="max").pack(side="left")
        self.max_box = ttk.Spinbox(spk, values=SPEAKER_CHOICES, textvariable=self.max_spk, width=5, state="readonly")
        self.max_box.pack(side="left", padx=4)

        ttk.Label(tf, text="Tip: large-v3-turbo suits an NVIDIA GPU; without one, 'small' or 'medium' "
                           "is much faster.", foreground="#777"
                  ).grid(row=3, column=0, columnspan=6, sticky="w", pady=(6, 0))

        # ---- run bar
        bottom = ttk.Frame(main)
        bottom.pack(fill="x", pady=(10, 0))
        self.progress = ttk.Progressbar(bottom, maximum=1.0)
        self.progress.pack(side="left", fill="x", expand=True)
        self.merge_btn = ttk.Button(bottom, text="Merge audio only...", command=lambda: self.start(False))
        self.merge_btn.pack(side="left", padx=(8, 0))
        self.go_btn = ttk.Button(bottom, text="Merge & transcribe...", command=lambda: self.start(True))
        self.go_btn.pack(side="left", padx=(6, 0))
        self.tx_btn = ttk.Button(bottom, text="Transcribe a file...", command=self.transcribe_existing)
        self.tx_btn.pack(side="left", padx=(6, 0))
        self.cancel_btn = ttk.Button(bottom, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=(6, 0))
        self.status = ttk.Label(main, text="Ready" if self.ffmpeg else
                                "ffmpeg not found - install it or run: pip install imageio-ffmpeg",
                                foreground="#555" if self.ffmpeg else "#b00")
        self.status.pack(anchor="w", pady=(4, 0))

        # ---- bindings
        if HAS_DND:
            for w in (root, self.tree):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self.on_drop)
        self.tree.bind("<ButtonPress-1>", self.on_press)
        self.tree.bind("<B1-Motion>", self.on_motion)
        self.tree.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag_item", None))
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        self.tree.bind("<Control-Up>", lambda e: (self.move(-1), "break")[1])
        self.tree.bind("<Control-Down>", lambda e: (self.move(1), "break")[1])
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        menubar = tk.Menu(root)
        settings_menu = tk.Menu(menubar, tearoff=False)
        settings_menu.add_command(label="Settings...", accelerator="Ctrl+,", command=self.open_settings)
        settings_menu.add_command(label="Hugging Face setup help", command=self.show_token_help)
        menubar.add_cascade(label="Settings", menu=settings_menu)
        root.config(menu=menubar)
        root.bind_all("<Control-comma>", lambda e: self.open_settings())
        self.settings_win = None

        self.update_controls()
        self.root.after(100, self.poll)

    # ------------------------------------------------------------ list management
    def add_paths(self, paths):
        files = []
        for p in map(Path, paths):
            if p.is_dir():
                files += sorted((f for f in p.iterdir() if f.suffix.lower() in AUDIO_EXTS), key=natural_key)
            elif p.is_file():
                files.append(p)
        if len(paths) > 1:  # several dropped at once: put part2 before part10
            files.sort(key=natural_key)
        new = []
        for f in files:
            iid = self.tree.insert("", "end", values=("", f.name, "...", fmt_size(f.stat().st_size)))
            self.paths[iid] = str(f)
            self.durations[iid] = None
            new.append(iid)
        self.renumber()
        if new and self.ffmpeg:
            items = [(i, self.paths[i]) for i in new]
            threading.Thread(target=self._probe_worker, args=(items,), daemon=True).start()

    def _probe_worker(self, items):
        for iid, path in items:
            self.q.put(("dur", iid, probe_duration(self.ffmpeg, path)))

    def add_dialog(self):
        files = filedialog.askopenfilenames(title="Choose audio files", filetypes=self.audio_filetypes())
        if files:
            self.add_paths(list(files))

    @staticmethod
    def audio_filetypes():
        return [("Audio", " ".join(f"*{e}" for e in sorted(AUDIO_EXTS))), ("All files", "*.*")]

    def on_drop(self, event):
        if not self.busy:
            self.add_paths(self.root.tk.splitlist(event.data))

    def remove_selected(self):
        for iid in self.tree.selection():
            self.tree.delete(iid)
            self.paths.pop(iid, None)
            self.durations.pop(iid, None)
        self.renumber()

    def clear(self):
        self.tree.delete(*self.tree.get_children())
        self.paths.clear()
        self.durations.clear()
        self.renumber()

    def move(self, direction):
        sel = list(self.tree.selection())
        if not sel:
            return
        items = list(self.tree.get_children())
        order = sorted(sel, key=items.index, reverse=direction > 0)
        for iid in order:
            new = self.tree.index(iid) + direction
            if 0 <= new < len(items):
                if self.tree.get_children()[new] in sel:  # blocked by another selected item at the edge
                    continue
                self.tree.move(iid, "", new)
        self.tree.see(sel[0])
        self.renumber()

    def sort_by_name(self):
        for i, iid in enumerate(sorted(self.tree.get_children(), key=lambda i: natural_key(self.paths[i]))):
            self.tree.move(iid, "", i)
        self.renumber()

    def on_press(self, event):
        self._drag_item = self.tree.identify_row(event.y) or None

    def on_motion(self, event):
        target = self.tree.identify_row(event.y)
        if self._drag_item and target and target != self._drag_item and not self.busy:
            self.tree.move(self._drag_item, "", self.tree.index(target))
            self.renumber()

    def renumber(self):
        for i, iid in enumerate(self.tree.get_children(), 1):
            self.tree.set(iid, "n", i)
        self.update_summary()

    def ordered_files(self):
        return [self.paths[i] for i in self.tree.get_children()]

    def update_summary(self):
        self.br_box.configure(state="readonly" if self.fmt.get() == "mp3" else "disabled")
        items = self.tree.get_children()
        if not items:
            self.summary.configure(text="No files yet.")
            return
        size = sum(os.path.getsize(self.paths[i]) for i in items)
        known = [self.durations[i] for i in items if self.durations[i] is not None]
        total = sum(known)
        text = f"{len(items)} file(s)  ·  total {fmt_time(total)}{'+' if len(known) < len(items) else ''}  ·  {fmt_size(size)}"
        if self.fmt.get() == "mp3" and total:
            kbps = int(self.bitrate.get().rstrip("k"))
            text += f"  ·  estimated MP3 size ≈ {fmt_size(total * kbps * 1000 / 8)}"
        self.summary.configure(text=text)

    def update_controls(self):
        spk_state = "readonly" if self.diarize.get() else "disabled"
        self.min_box.configure(state=spk_state)
        self.max_box.configure(state=spk_state)
        if not self.diarize.get():
            self.token_label.configure(text="Speakers won't be labelled", foreground="#777")
        elif self.token.get().strip():
            self.token_label.configure(text="\u2713 Hugging Face token set", foreground="#070")
        else:
            self.token_label.configure(text="\u26a0 No Hugging Face token - add it in Settings", foreground="#b60")
        self.update_summary()

    def open_settings(self):
        if self.settings_win and self.settings_win.winfo_exists():
            self.settings_win.lift()
            self.settings_win.focus_set()
            return
        self.settings_win = SettingsDialog(self)

    def show_token_help(self):
        if messagebox.askokcancel(
                "Hugging Face token",
                "Speaker identification uses the free pyannote model, which needs a one-time setup:\n\n"
                "1. Create a free account at huggingface.co\n"
                f"2. Open {tr.TERMS_URL}\n   and accept the user conditions\n"
                f"3. Create a 'Read' token at {tr.TOKEN_URL}\n   and paste it in Settings\n\n"
                "After the first download the models are cached and everything runs offline.\n\n"
                "Open both pages in your browser now?"):
            webbrowser.open(tr.TERMS_URL)
            webbrowser.open(tr.TOKEN_URL)

    # ------------------------------------------------------------ settings
    def remember_settings(self):
        cfg = {"format": self.fmt.get(), "bitrate": self.bitrate.get(), "mono": self.mono.get(),
               "diarize": self.diarize.get(),
               "model": self.model.get(), "language": self.language.get(),
               "min_speakers": self.min_spk.get(), "max_speakers": self.max_spk.get()}
        if self.remember_token.get() and self.token.get().strip():
            cfg["hf_token"] = self.token.get().strip()
        save_config(cfg)

    def check_transcription_ready(self):
        if importlib.util.find_spec("whisperx") is None:
            messagebox.showerror(
                "WhisperX not installed",
                f"Transcription needs WhisperX in the Python that runs this app:\n{sys.executable}\n\n"
                "Install it with:\n    pip install whisperx\n\nSee README.md for GPU (CUDA) setup.")
            return False
        if self.diarize.get() and not self.token.get().strip():
            answer = messagebox.askyesnocancel(
                "No Hugging Face token",
                "Speaker identification needs a Hugging Face token.\n\n"
                "Yes  -  open Settings to add it\n"
                "No  -  transcribe without speaker labels this time")
            if answer is None:
                return False
            if answer:
                self.open_settings()
                return False
        return True

    # ------------------------------------------------------------ running
    def set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in self.side_buttons + [self.merge_btn, self.go_btn, self.tx_btn]:
            b.configure(state=state)
        self.cancel_btn.configure(state="normal" if busy else "disabled")
        if not busy:
            self.proc = None
            self.progress.configure(mode="determinate")

    def set_status(self, text, color="#555"):
        self.status.configure(text=text, foreground=color)

    def start(self, transcribe=False):
        if not self.ffmpeg:
            messagebox.showerror("ffmpeg missing", "ffmpeg was not found.\n\nRun:  pip install imageio-ffmpeg\n"
                                 "or install ffmpeg and add it to PATH.")
            return
        files = self.ordered_files()
        if not files:
            messagebox.showinfo("Nothing to do", "Add some files first.")
            return
        if transcribe and not self.check_transcription_ready():
            return
        fmt = self.fmt.get()
        first = Path(files[0])
        out = filedialog.asksaveasfilename(title="Save combined file as", defaultextension=f".{fmt}",
                                           initialdir=str(first.parent),
                                           initialfile=f"{first.stem}_combined.{fmt}",
                                           filetypes=[(fmt.upper(), f"*.{fmt}")])
        if not out:
            return
        if any(Path(out).resolve() == Path(f).resolve() for f in files):
            messagebox.showerror("Invalid output", "The output file can't be one of the input files.")
            return
        self.remember_settings()
        total = sum(d for d in self.durations.values() if d) or None
        self.cancelled = False
        self.set_busy(True)
        self.progress["value"] = 0
        args = (self.ffmpeg, files, out, fmt, self.bitrate.get(), self.mono.get(), total,
                lambda p: self.q.put(("progress", p)), lambda s: self.q.put(("status", s)),
                lambda proc: setattr(self, "proc", proc))
        threading.Thread(target=self._join_job, args=(args, out, transcribe), daemon=True).start()

    def _join_job(self, args, out, then_transcribe):
        try:
            ok, log = join_files(*args)
        except Exception as e:
            ok, log = False, str(e)
        self.q.put(("joined", ok, log, out, then_transcribe))

    def transcribe_existing(self):
        path = filedialog.askopenfilename(title="Choose a recording to transcribe", filetypes=self.audio_filetypes())
        if not path or not self.check_transcription_ready():
            return
        self.remember_settings()
        self.cancelled = False
        self.set_busy(True)
        self.start_transcription(path)

    def start_transcription(self, audio):
        txt = unique_path(Path(audio).with_suffix(".txt"))
        json_path = Path(tempfile.gettempdir()) / f"transcript_{os.getpid()}_{threading.get_ident()}.json"
        lang = self.language.get().split()[0] if self.language.get().strip() else "auto-detect"
        # When the app was started windowless (pythonw.exe), run the worker with python.exe:
        # it reports progress through stdout, and CREATE_NO_WINDOW keeps it hidden anyway.
        exe = Path(sys.executable)
        if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
            exe = exe.parent / "python.exe"
        cmd = [str(exe), "-u", str(HERE / "transcriber.py"), str(audio), "-o", str(txt),
               "--json", str(json_path), "--model", self.model.get()]
        if lang != "auto-detect":
            cmd += ["--language", lang]
        token = self.token.get().strip()
        if not self.diarize.get() or not token:
            cmd.append("--no-diarize")
        else:
            for flag, var in (("--min-speakers", self.min_spk), ("--max-speakers", self.max_spk)):
                if var.get().isdigit():
                    cmd += [flag, var.get()]
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        if token:
            env["HF_TOKEN"] = token  # passed via environment, not the command line
        self.progress["value"] = 0
        self.set_status("Transcript: starting...")
        threading.Thread(target=self._transcribe_job, args=(cmd, env, txt, json_path), daemon=True).start()

    def _transcribe_job(self, cmd, env, txt, json_path):
        tail, warnings, done = deque(maxlen=40), [], None
        try:
            self.proc = proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                stdin=subprocess.DEVNULL, env=env, text=True, encoding="utf-8",
                                                errors="replace", creationflags=NO_WINDOW)
            for line in proc.stdout:  # universal newlines also splits tqdm's \r updates
                line = line.rstrip()
                if line.startswith("@@"):
                    kind, _, val = line[2:].partition("\t")
                    if kind == "stage":
                        self.q.put(("status", "Transcript: " + val))
                    elif kind == "progress":
                        self.q.put(("progress", float(val) / 100))
                    elif kind == "warn":
                        warnings.append(val)
                    elif kind == "done":
                        done = val
                    continue
                if line:
                    tail.append(line)
                m = re.match(r"\s*([^:|]{1,60}):\s*(\d+)%\|", line)  # huggingface download bars
                if m:
                    self.q.put(("substatus", f"downloading {m[1].strip()} {m[2]}%"))
            proc.wait()
            data = None
            if proc.returncode == 0 and done and json_path.exists():
                data = json.loads(json_path.read_text(encoding="utf-8"))
            self.q.put(("transcribed", data is not None, txt, data, "\n".join(tail), warnings))
        except Exception as e:
            self.q.put(("transcribed", False, txt, None, str(e), warnings))
        finally:
            try:
                json_path.unlink()
            except OSError:
                pass

    def cancel(self):
        self.cancelled = True
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()
        self.set_status("Cancelling...")

    def on_close(self):
        if self.busy and not messagebox.askyesno("Quit?", "A job is still running. Stop it and quit?"):
            return
        self.cancel()
        self.root.destroy()

    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "dur":
                    _, iid, dur = msg
                    if iid in self.paths:
                        self.durations[iid] = dur
                        self.tree.set(iid, "dur", fmt_time(dur))
                        self.update_summary()
                elif kind == "progress":
                    self.progress["value"] = msg[1]
                elif kind == "status":
                    self._stage = msg[1]
                    self.set_status(msg[1])
                elif kind == "substatus":
                    self.set_status(f"{getattr(self, '_stage', '')}  ({msg[1]})")
                elif kind == "joined":
                    self.on_joined(*msg[1:])
                elif kind == "transcribed":
                    self.on_transcribed(*msg[1:])
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def on_joined(self, ok, log, out, then_transcribe):
        if self.cancelled:
            try:
                os.remove(out)  # remove the half-written file
            except OSError:
                pass
            self.set_busy(False)
            self.set_status("Cancelled.")
            return
        if not ok:
            self.set_busy(False)
            self.set_status("Combining failed - see error message", "#b00")
            messagebox.showerror("ffmpeg error", "\n".join(log.strip().splitlines()[-15:]) or "Unknown error")
            return
        self.progress["value"] = 1.0
        size = fmt_size(os.path.getsize(out))
        if then_transcribe:
            self.set_status(f"Combined: {out} ({size}). Starting transcript...", "#070")
            self.start_transcription(out)
            return
        self.set_busy(False)
        self.set_status(f"Done: {out}  ({size})", "#070")
        if messagebox.askyesno("Done", f"Saved:\n{out}\n\nOpen the containing folder?"):
            self.open_folder(out)

    def on_transcribed(self, ok, txt, data, log, warnings):
        self.set_busy(False)
        if self.cancelled:
            self.set_status("Transcription cancelled.")
            return
        if not ok:
            self.set_status("Transcription failed - see error message", "#b00")
            messagebox.showerror("Transcription error", "\n".join(log.strip().splitlines()[-20:]) or "Unknown error")
            return
        self.progress["value"] = 1.0
        if data.get("diarized") and tr.speaker_order(data):
            dlg = SpeakerDialog(self.root, data)
            self.root.wait_window(dlg)
            if dlg.result:
                tr.write_transcript(data, txt, dlg.result)
        self.set_status(f"Transcript saved: {txt}", "#070")
        note = ("\n\nNote:\n" + "\n".join(f"- {w}" for w in warnings)) if warnings else ""
        if messagebox.askyesno("Transcript ready", f"Saved:\n{txt}{note}\n\nOpen it now?"):
            open_file(txt)

    @staticmethod
    def open_folder(path):
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(Path(path))])
        else:
            open_file(Path(path).parent)


def main():
    root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
