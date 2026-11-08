"""
Meeting transcriber: WhisperX (speech-to-text with word timing) + pyannote (who spoke when).
Everything runs locally; models are downloaded once from Hugging Face and then cached.

Command line:
    python transcriber.py meeting.m4a                     -> meeting.txt
    python transcriber.py meeting.m4a -o notes.txt --model large-v3-turbo --language en
    python transcriber.py meeting.m4a --min-speakers 2 --max-speakers 5
    python transcriber.py meeting.m4a --no-diarize        (no Hugging Face token needed)

Speaker identification needs a Hugging Face token (--hf-token or the HF_TOKEN environment
variable) and you must accept the terms of pyannote/speaker-diarization-community-1 once.

m4a_joiner.py runs this file as a subprocess. Lines starting with "@@" are progress
messages for the GUI; everything else is ordinary log output.
"""
import argparse
import datetime
import gc
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SAMPLE_RATE = 16000
MODELS = ["tiny", "base", "small", "medium", "large-v3-turbo", "large-v3"]
DIARIZATION_MODEL = "pyannote/speaker-diarization-community-1"
TERMS_URL = "https://huggingface.co/" + DIARIZATION_MODEL
TOKEN_URL = "https://huggingface.co/settings/tokens"
NO_SPACE_LANGS = {"zh", "ja", "th", "lo", "km", "my", "yue", "bo"}  # text is joined without spaces
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Share of the overall progress bar given to each step.
STEPS = {"load": (0, 5), "transcribe": (5, 60), "align": (60, 75), "diarize": (75, 98)}


# --------------------------------------------------------------------------- progress protocol
def emit(kind, value=""):
    print(f"@@{kind}\t{value}", flush=True)


def step_progress(step):
    lo, hi = STEPS[step]
    return lambda pct: emit("progress", f"{lo + (hi - lo) * min(max(pct, 0), 100) / 100:.1f}")


# --------------------------------------------------------------------------- helpers
def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def load_audio(path, ffmpeg=None):
    """Decode any audio file to 16 kHz mono float32, the format WhisperX and pyannote expect.
    Done here (rather than by whisperx/pyannote) so it works with the imageio-ffmpeg binary
    and avoids torchcodec decoding problems on Windows."""
    import numpy as np
    ffmpeg = ffmpeg or find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found. Run: pip install imageio-ffmpeg")
    cmd = [ffmpeg, "-nostdin", "-threads", "0", "-i", str(path), "-f", "s16le", "-ac", "1",
           "-acodec", "pcm_s16le", "-ar", str(SAMPLE_RATE), "-"]
    r = subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError("Could not decode audio:\n" + r.stderr.decode(errors="replace")[-800:])
    return np.frombuffer(r.stdout, np.int16).flatten().astype(np.float32) / 32768.0


def free_memory():
    """Call after `del model` so GPU/CPU memory is released before the next model loads."""
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def fmt_ts(sec):
    sec = int(sec or 0)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def fmt_dur(sec):
    sec = int(round(sec or 0))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# --------------------------------------------------------------------------- pipeline
def transcribe(audio_path, model_name="large-v3-turbo", language=None, device="auto",
               diarize=True, hf_token=None, min_speakers=None, max_speakers=None, batch_size=None):
    """Run WhisperX (+ pyannote). Returns a plain dict that format_transcript() turns into text."""
    emit("stage", "Loading audio...")
    audio = load_audio(audio_path)
    duration = len(audio) / SAMPLE_RATE
    emit("progress", 2)

    emit("stage", "Loading WhisperX (first start can take a minute)...")
    import torch
    import whisperx

    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    compute_type = "float16" if device == "cuda" else "int8"
    batch_size = batch_size or (16 if device == "cuda" else 8)
    emit("info", f"Device: {device.upper()} ({compute_type}), model: {model_name}")
    warnings = []

    # 1. Speech -> text
    emit("stage", f"Loading Whisper model '{model_name}' (downloaded on first use)...")
    model = whisperx.load_model(model_name, device, compute_type=compute_type, language=language or None)
    emit("progress", STEPS["load"][1])
    emit("stage", f"Transcribing {fmt_dur(duration)} of audio on {device.upper()}...")
    result = model.transcribe(audio, batch_size=batch_size, language=language or None,
                              progress_callback=step_progress("transcribe"))
    lang = result.get("language") or language or "en"
    del model
    free_memory()
    emit("info", f"Language: {lang}, {len(result['segments'])} segments")

    # 2. Word-level timing (makes speaker assignment much more accurate)
    emit("stage", "Aligning words to the audio...")
    try:
        align_model, metadata = whisperx.load_align_model(language_code=lang, device=device)
        result = whisperx.align(result["segments"], align_model, metadata, audio, device,
                                return_char_alignments=False, progress_callback=step_progress("align"))
        del align_model
        free_memory()
    except Exception as e:  # no alignment model for this language, etc.
        warnings.append(f"Word alignment skipped ({e}); speaker turns use sentence timing.")
        emit("warn", warnings[-1])

    # 3. Who spoke when
    speakers_found = False
    if diarize:
        emit("stage", "Identifying speakers (pyannote)...")
        emit("progress", STEPS["diarize"][0])
        try:
            from whisperx.diarize import DiarizationPipeline
            pipe = DiarizationPipeline(token=hf_token or None, device=device)
            if getattr(pipe, "model", True) is None:
                raise RuntimeError("model could not be downloaded")
            diarize_df = pipe(audio, min_speakers=min_speakers or None, max_speakers=max_speakers or None,
                              progress_callback=step_progress("diarize"))
            result = whisperx.assign_word_speakers(diarize_df, result, fill_nearest=True)
            speakers_found = True
            del pipe
            free_memory()
        except Exception as e:
            msg = (f"Speaker identification failed: {e}\n"
                   f"Check that your Hugging Face token is valid ({TOKEN_URL}) and that you accepted "
                   f"the model terms at {TERMS_URL}. The transcript was saved without speakers.")
            warnings.append(msg)
            emit("warn", msg.replace("\n", " "))

    segments = []
    for s in result["segments"]:
        text = (s.get("text") or "").strip()
        if text:
            segments.append({"start": float(s.get("start") or 0), "end": float(s.get("end") or 0),
                             "text": text, "speaker": s.get("speaker")})
    emit("progress", 99)
    return {"source": str(audio_path), "language": lang, "duration": duration, "model": model_name,
            "diarized": speakers_found, "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "segments": segments, "warnings": warnings}


# --------------------------------------------------------------------------- output
def speaker_order(data):
    """Raw speaker labels in order of first appearance."""
    seen = []
    for s in data["segments"]:
        if s.get("speaker") and s["speaker"] not in seen:
            seen.append(s["speaker"])
    return seen


def default_names(data):
    return {spk: f"Speaker {i}" for i, spk in enumerate(speaker_order(data), 1)}


def speaker_stats(data):
    """{label: (talk_seconds, [longest few utterances])} - used by the GUI naming dialog."""
    stats = {}
    for s in data["segments"]:
        spk = s.get("speaker")
        if spk:
            t, segs = stats.setdefault(spk, [0.0, []])
            stats[spk][0] = t + max(0.0, s["end"] - s["start"])
            segs.append(s)
    return {k: (t, sorted(segs, key=lambda s: -len(s["text"]))[:3]) for k, (t, segs) in stats.items()}


def format_transcript(data, names=None):
    names = {**default_names(data), **(names or {})}
    sep = "" if data.get("language") in NO_SPACE_LANGS else " "

    # Merge consecutive segments of the same speaker (by name, so renamed duplicates merge too).
    # Long monologues / unlabelled stretches are split into paragraphs at pauses, each with a timestamp.
    blocks = []
    for s in data["segments"]:
        who = names.get(s.get("speaker")) if s.get("speaker") else (blocks[-1]["who"] if blocks else None)
        b = blocks[-1] if blocks else None
        long_block = b and s["start"] - b["start"] > (120 if who else 45)
        pause = b and s["start"] - b["end"] > (8 if who else 2.5)
        if b and b["who"] == who and not long_block and not pause:
            b["text"] += sep + s["text"]
            b["end"] = s["end"]
        else:
            blocks.append({"start": s["start"], "end": s["end"], "who": who, "text": s["text"]})

    lines = ["MEETING TRANSCRIPT", "",
             f"Recording: {Path(data['source']).name}",
             f"Created:   {data['created']}",
             f"Length:    {fmt_dur(data['duration'])}",
             f"Language:  {data['language']}"]
    people = list(dict.fromkeys(names[s] for s in speaker_order(data)))
    if people:
        lines.append(f"Speakers:  {', '.join(people)}")
    lines += ["", "-" * 60, ""]
    for b in blocks:
        lines.append(f"[{fmt_ts(b['start'])}] {b['who']}:" if b["who"] else f"[{fmt_ts(b['start'])}]")
        lines.append(b["text"])
        lines.append("")
    return "\n".join(lines)


def write_transcript(data, path, names=None):
    Path(path).write_text(format_transcript(data, names), encoding="utf-8")


# --------------------------------------------------------------------------- downloaded models
# Where each model comes from (matches faster-whisper 1.2 / whisperx 3.8 / torchaudio 2.8).
WHISPER_REPOS = {
    "tiny": "Systran/faster-whisper-tiny", "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small", "medium": "Systran/faster-whisper-medium",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo", "large-v3": "Systran/faster-whisper-large-v3",
}
ALIGN_TORCH_FILES = {  # downloaded by torchaudio into the PyTorch hub cache
    "en": "wav2vec2_fairseq_base_ls960_asr_ls960.pth", "fr": "wav2vec2_voxpopuli_base_10k_asr_fr.pt",
    "de": "wav2vec2_voxpopuli_base_10k_asr_de.pt", "es": "wav2vec2_voxpopuli_base_10k_asr_es.pt",
    "it": "wav2vec2_voxpopuli_base_10k_asr_it.pt",
}
ALIGN_HF_REPOS = {  # downloaded from Hugging Face
    "zh": "jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn", "ja": "jonatasgrosman/wav2vec2-large-xlsr-53-japanese",
    "nl": "jonatasgrosman/wav2vec2-large-xlsr-53-dutch", "pt": "jonatasgrosman/wav2vec2-large-xlsr-53-portuguese",
    "ar": "jonatasgrosman/wav2vec2-large-xlsr-53-arabic", "ru": "jonatasgrosman/wav2vec2-large-xlsr-53-russian",
    "pl": "jonatasgrosman/wav2vec2-large-xlsr-53-polish", "tr": "mpoyraz/wav2vec2-xls-r-300m-cv7-turkish",
    "ko": "kresnik/wav2vec2-large-xlsr-korean", "hi": "theainerd/Wav2Vec2-large-xlsr-hindi",
    "sv": "KBLab/wav2vec2-large-voxrex-swedish",
}


def hf_cache_dir():
    try:
        from huggingface_hub import constants
        return Path(constants.HF_HUB_CACHE)
    except Exception:
        home = os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")
        return Path(os.environ.get("HF_HUB_CACHE") or os.path.join(home, "hub"))


def torch_hub_dir():
    """Same rule as torch.hub.get_dir(), without importing torch."""
    if os.environ.get("TORCH_HOME"):
        return Path(os.environ["TORCH_HOME"]) / "hub"
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "torch" / "hub"


def model_inventory(languages=None):
    """List the models this app can use and whether each is already downloaded.
    Returns dicts: group, name, source, downloaded, size (bytes), path (folder or file to show)."""
    repos = {}
    try:
        from huggingface_hub import scan_cache_dir
        for r in scan_cache_dir(hf_cache_dir()).repos:
            if r.repo_type == "model":
                repos[r.repo_id] = r
    except Exception:  # no cache yet, or huggingface_hub missing
        pass

    def hf_item(group, name, repo_id, must_have=None):
        r = repos.get(repo_id)
        path, ok, size = hf_cache_dir() / ("models--" + repo_id.replace("/", "--")), False, 0
        if r and r.revisions:
            rev = max(r.revisions, key=lambda v: v.last_modified)
            names = {f.file_name for f in rev.files}
            ok = bool(names) and (must_have is None or must_have in names)
            path, size = rev.snapshot_path, r.size_on_disk
        return {"group": group, "name": name, "source": repo_id, "downloaded": ok, "size": size, "path": str(path)}

    items = [hf_item("Speech-to-text (Whisper)", m, WHISPER_REPOS[m], "model.bin") for m in MODELS]
    items.append(hf_item("Speaker identification (pyannote)", "speaker-diarization-community-1", DIARIZATION_MODEL))

    langs = list(dict.fromkeys(languages or (list(ALIGN_TORCH_FILES) + list(ALIGN_HF_REPOS))))
    ckpt = torch_hub_dir() / "checkpoints"
    for lang in langs:
        group = "Word alignment (per language)"
        if lang in ALIGN_TORCH_FILES:
            f = ckpt / ALIGN_TORCH_FILES[lang]
            items.append({"group": group, "name": lang, "source": "torchaudio: " + f.name, "downloaded": f.exists(),
                          "size": f.stat().st_size if f.exists() else 0, "path": str(f if f.exists() else ckpt)})
        elif lang in ALIGN_HF_REPOS:
            items.append(hf_item(group, lang, ALIGN_HF_REPOS[lang]))
    return items


# --------------------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="Transcribe a meeting recording with speaker labels.")
    ap.add_argument("audio")
    ap.add_argument("-o", "--output", help="output .txt (default: next to the audio)")
    ap.add_argument("--model", default="large-v3-turbo", help=f"one of {', '.join(MODELS)}")
    ap.add_argument("--language", default=None, help="e.g. en, zh, fr (default: auto-detect)")
    ap.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    ap.add_argument("--batch-size", type=int, default=None)
    ap.add_argument("--no-diarize", action="store_true", help="skip speaker identification")
    ap.add_argument("--hf-token", default=None, help="Hugging Face token (or set HF_TOKEN)")
    ap.add_argument("--min-speakers", type=int, default=None)
    ap.add_argument("--max-speakers", type=int, default=None)
    ap.add_argument("--json", help="also save the raw result (used by the GUI for speaker naming)")
    a = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    out = Path(a.output) if a.output else Path(a.audio).with_suffix(".txt")
    token = a.hf_token or os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
    if not a.no_diarize and not token:
        emit("warn", "No Hugging Face token given - speakers may not be identified.")

    data = transcribe(a.audio, a.model, a.language, a.device, not a.no_diarize, token,
                      a.min_speakers, a.max_speakers, a.batch_size)
    write_transcript(data, out)
    if a.json:
        Path(a.json).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    emit("progress", 100)
    emit("done", str(out))


if __name__ == "__main__":
    main()
