"""
First-run setup for Meeting Transcriber, run by the launchers before the app starts.

  * installs everything in requirements.txt into the virtual environment
  * if an NVIDIA GPU is present, offers to switch PyTorch to the CUDA (GPU) build

When everything is already installed it finishes in a fraction of a second, so it is
safe to run on every start. Delete the .venv folder (or run with --force) to redo setup.
"""
import hashlib
import importlib.metadata as md
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REQUIREMENTS = HERE / "requirements.txt"
MARKER = Path(sys.prefix) / ".meeting_transcriber_setup.json"  # lives inside the venv
CUDA_INDEX = "https://download.pytorch.org/whl/cu128"
SUPPORTED = ((3, 10), (3, 13))


def say(msg=""):
    print(msg, flush=True)


def requirement_names():
    names = []
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line and not line.startswith("-"):
            names.append(re.split(r"[<>=!~;\[\s]", line, maxsplit=1)[0])
    return names


def missing_packages():
    missing = []
    for name in requirement_names():
        try:
            md.version(name)
        except md.PackageNotFoundError:
            missing.append(name)
    return missing


def requirements_hash():
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()[:16]


def load_marker():
    try:
        return json.loads(MARKER.read_text(encoding="utf-8"))
    except Exception:
        return {}


def ask_yes(question, default=True):
    hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{question} {hint} ").strip().lower()
    except EOFError:  # not interactive
        return default
    return default if not answer else answer.startswith("y")


def pip(*args):
    return subprocess.call([sys.executable, "-m", "pip", *args])


# --------------------------------------------------------------------------- GPU
def nvidia_gpu_name():
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    try:
        out = subprocess.run([smi, "--query-gpu=name", "--format=csv,noheader"], capture_output=True,
                             text=True, timeout=20)
        return out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else None
    except Exception:
        return None


def torch_status():
    """(version, cuda_available) of the installed torch, checked in a fresh process."""
    code = "import json, torch; print(json.dumps([torch.__version__, torch.cuda.is_available()]))"
    try:
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300)
        return tuple(json.loads(out.stdout.strip().splitlines()[-1]))
    except Exception:
        return None


def install_cuda_torch():
    """Swap the CPU-only PyTorch for the CUDA build of the same versions."""
    pins = []
    for name in ("torch", "torchaudio", "torchvision"):
        try:
            pins.append(f"{name}=={md.version(name).split('+')[0]}")
        except md.PackageNotFoundError:
            pass
    if not pins:
        return False
    say(f"\nDownloading the GPU build of {', '.join(pins)} (about 2.5 GB)...\n")
    return pip("install", "--force-reinstall", "--no-deps", *pins, "--index-url", CUDA_INDEX) == 0


def gpu_step(marker):
    if sys.platform not in ("win32", "linux") or marker.get("gpu") in ("declined", "installed"):
        return marker.get("gpu")
    gpu = nvidia_gpu_name()
    status = torch_status()
    if not gpu or not status:
        return "none"
    version, cuda = status
    if cuda:
        say(f"GPU: {gpu} - PyTorch {version} already uses it.")
        return "installed"
    say(f"\nFound an NVIDIA GPU: {gpu}")
    say("Transcription is many times faster on the GPU, but PyTorch is currently the CPU-only build.")
    if not ask_yes("Download the GPU build of PyTorch now (about 2.5 GB)?"):
        say("OK - staying on CPU. Run setup_check.py --force later to change this.")
        return "declined"
    if install_cuda_torch() and (torch_status() or (None, False))[1]:
        say("GPU build installed and working.")
        return "installed"
    say("\nThe GPU build could not be installed or doesn't see the GPU (old driver?).\n"
        "The app still works on CPU. Update your NVIDIA driver and run setup_check.py --force to retry.")
    return "failed"


# --------------------------------------------------------------------------- main
def main(argv):
    force = "--force" in argv
    marker = {} if force else load_marker()
    if not force and marker.get("requirements") == requirements_hash() and not missing_packages():
        return 0  # already set up

    say("=" * 64)
    say(" Meeting Transcriber - setup")
    say("=" * 64)
    if sys.prefix == sys.base_prefix:
        say("\nWarning: this is not running inside a virtual environment, so packages would be\n"
            f"installed into {sys.executable}. Use the Start script instead.")
        if not ask_yes("Install here anyway?", default=False):
            return 1

    v = sys.version_info[:2]
    if not SUPPORTED[0] <= v <= SUPPORTED[1]:
        say(f"\nPython {v[0]}.{v[1]} is not supported - WhisperX needs Python 3.10 to 3.13.\n"
            "Install Python 3.12 from https://www.python.org/downloads/ , delete the .venv folder\n"
            "and start again.")
        return 1
    try:
        import tkinter  # noqa: F401
    except ImportError:
        say("\nThis Python has no tkinter (needed for the window). On Windows reinstall Python from\n"
            "python.org with 'tcl/tk and IDLE' ticked; on Linux install python3-tk.")
        return 1

    say("\nInstalling packages - the first time this downloads about 1 GB and takes a few minutes...\n")
    pip("install", "--upgrade", "pip", "--quiet")
    if pip("install", "-r", str(REQUIREMENTS)) != 0 or missing_packages():
        say("\nPackage installation failed. Common causes:\n"
            "  - no internet connection, or a VPN/proxy blocking pypi.org\n"
            "  - an old extra package source in your pip settings (check with: pip config list)\n"
            "Fix the problem and start again - setup continues where it stopped.")
        return 1

    marker = {**marker, "requirements": requirements_hash()}
    marker["gpu"] = gpu_step(marker)
    MARKER.write_text(json.dumps(marker, indent=2), encoding="utf-8")
    say("\nSetup complete. Models are downloaded automatically the first time you transcribe.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
