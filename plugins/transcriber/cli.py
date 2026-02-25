"""Voice transcription CLI with streaming support."""

from dotenv import load_dotenv

load_dotenv()

import platform
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel

app = typer.Typer(help="Local-first voice transcription with live streaming")
console = Console()

IS_MACOS = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"

MODELS = {
    "tiny": ("mlx-community/whisper-tiny", "tiny"),
    "base": ("mlx-community/whisper-base", "base"),
    "small": ("mlx-community/whisper-small", "small"),
    "medium": ("mlx-community/whisper-medium", "medium"),
    "large": ("mlx-community/whisper-large-v3", "large-v3"),
    "turbo": ("mlx-community/whisper-large-v3-turbo", "large-v3-turbo"),
}
DEFAULT_MODEL = "turbo"
END_PHRASE = "over and out"

# Common Whisper hallucinations on silence
HALLUCINATIONS = {
    "thank you",
    "thanks for watching",
    "thanks for listening",
    "subscribe",
    "like and subscribe",
    "see you next time",
    "bye",
    "goodbye",
    "thank you for watching",
    "you",
    ".",
    "",
    " ",
}


def get_model_name(model: str) -> str:
    """Get platform-specific model name."""
    if model in MODELS:
        return MODELS[model][0] if IS_MACOS else MODELS[model][1]
    return model


def find_recorder() -> str:
    """Find available audio recorder."""
    if shutil.which("sox") or shutil.which("rec"):
        return "sox"
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    if shutil.which("arecord"):
        return "arecord"

    if IS_MACOS:
        raise typer.Exit("No audio recorder found. Install sox: brew install sox")
    else:
        raise typer.Exit("No audio recorder found. Install sox: apt install sox")


def get_default_audio_device() -> str:
    """Get the default audio input device index for macOS."""
    if not IS_MACOS:
        return "default"

    # List devices and find the first real microphone
    result = subprocess.run(
        ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True,
        text=True,
    )
    output = result.stderr

    # Look for microphone in the audio devices section
    # Format: [AVFoundation ...] [0] Device Name
    import re

    in_audio = False
    for line in output.split("\n"):
        if "audio devices" in line.lower():
            in_audio = True
            continue
        if in_audio:
            # Match pattern like [1] MacBook Pro Microphone
            match = re.search(r"\[(\d+)\]\s+(.+)", line)
            if match:
                idx, name = match.groups()
                # Prefer real microphone, skip virtual devices
                name_lower = name.lower()
                if "microphone" in name_lower and "virtual" not in name_lower:
                    return f":{idx}"

    # Fallback to device 1 (usually the built-in mic)
    return ":1"


def record_chunk_ffmpeg(output: Path, duration: float):
    """Record a chunk of audio using ffmpeg."""
    if IS_MACOS:
        device = get_default_audio_device()
        input_device = ["-f", "avfoundation", "-i", device]
    else:
        input_device = ["-f", "alsa", "-i", "default"]

    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "quiet",
        *input_device,
        "-ar",
        "16000",
        "-ac",
        "1",
        "-acodec",
        "pcm_s16le",
        "-t",
        str(duration),
        str(output),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def record_chunk_sox(output: Path, duration: float):
    """Record a chunk using sox."""
    cmd = [
        "rec",
        "-q",
        "-r",
        "16000",
        "-c",
        "1",
        "-b",
        "16",
        str(output),
        "trim",
        "0",
        str(duration),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def record_chunk(output: Path, duration: float):
    """Record a chunk of audio."""
    recorder = find_recorder()
    if recorder == "sox":
        record_chunk_sox(output, duration)
    else:
        record_chunk_ffmpeg(output, duration)


def is_hallucination(text: str) -> bool:
    """Check if text is a known Whisper hallucination."""
    cleaned = text.lower().strip().rstrip(".!?,")
    return cleaned in HALLUCINATIONS or len(cleaned) < 2


# Global model cache
_model_cache: dict = {}


def get_whisper_model(model: str):
    """Get or create cached Whisper model."""
    model_name = get_model_name(model)

    if model_name in _model_cache:
        return _model_cache[model_name]

    with console.status(f"[bold blue]Loading model {model_name}...[/]"):
        if IS_MACOS:
            import mlx_whisper

            # Warm up by loading the model (it caches internally)
            # Create a tiny silent audio to force model load
            tmp = Path(tempfile.mktemp(suffix=".wav"))
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "anullsrc=r=16000:cl=mono",
                    "-t",
                    "0.1",
                    "-acodec",
                    "pcm_s16le",
                    str(tmp),
                ],
                capture_output=True,
                check=True,
            )
            try:
                mlx_whisper.transcribe(str(tmp), path_or_hf_repo=model_name)
            finally:
                tmp.unlink(missing_ok=True)
            _model_cache[model_name] = ("mlx", model_name)
        else:
            from faster_whisper import WhisperModel

            whisper = WhisperModel(model_name, device="auto", compute_type="auto")
            _model_cache[model_name] = ("faster", whisper)

    console.print("[dim]Model loaded[/]")
    return _model_cache[model_name]


def transcribe_audio(path: Path, model: str, language: Optional[str] = None) -> str:
    """Transcribe audio file."""
    cached = get_whisper_model(model)

    if cached[0] == "mlx":
        import mlx_whisper

        result = mlx_whisper.transcribe(
            str(path),
            path_or_hf_repo=cached[1],
            language=language,
            condition_on_previous_text=False,
        )
        text = result["text"].strip()
    else:
        whisper = cached[1]
        segments, _ = whisper.transcribe(
            str(path),
            language=language,
            condition_on_previous_text=False,
        )
        text = " ".join(seg.text for seg in segments).strip()

    return "" if is_hallucination(text) else text


def merge_wav_files(files: list[Path], output: Path):
    """Merge multiple WAV files into one."""
    if not files:
        return

    if len(files) == 1:
        shutil.copy(files[0], output)
        return

    # Use sox to concatenate
    if shutil.which("sox"):
        cmd = ["sox"] + [str(f) for f in files] + [str(output)]
        subprocess.run(cmd, check=True, capture_output=True)
    else:
        # Use ffmpeg
        list_file = output.parent / "concat.txt"
        with open(list_file, "w") as f:
            for file in files:
                f.write(f"file '{file}'\n")
        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            str(output),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        list_file.unlink(missing_ok=True)


def _recorder_thread(
    tmpdir: Path, chunk_size: float, chunk_queue: "queue.Queue", stop_event: threading.Event
):
    """Background thread that continuously records audio chunks."""
    chunk_num = 0
    while not stop_event.is_set():
        chunk_path = tmpdir / f"chunk_{chunk_num:04d}.wav"
        try:
            record_chunk(chunk_path, chunk_size)
            if chunk_path.exists() and chunk_path.stat().st_size > 500:
                chunk_queue.put(chunk_path)
            chunk_num += 1
        except Exception:
            break


@app.command()
def record(
    output: Optional[Path] = typer.Option(
        None, "-o", "--output", help="Save transcription to file"
    ),
    model: str = typer.Option(
        DEFAULT_MODEL, "-m", "--model", help="Model: tiny/base/small/medium/large/turbo"
    ),
    duration: Optional[float] = typer.Option(
        None, "-d", "--duration", help="Max recording duration in seconds"
    ),
    language: Optional[str] = typer.Option(
        None, "-l", "--language", help="Language code (e.g., en, es)"
    ),
    copy: bool = typer.Option(False, "-c", "--copy", help="Copy result to clipboard"),
    chunk_size: float = typer.Option(5.0, "--chunk", help="Chunk size for streaming (seconds)"),
    streaming: bool = typer.Option(
        True, "--stream/--no-stream", help="Enable live streaming transcription"
    ),
):
    """Record with live streaming transcription. Say 'over and out' to stop."""
    import queue

    if not streaming:
        _record_simple(output, model, duration, language, copy)
        return

    # Pre-load model before recording
    get_whisper_model(model)

    tmpdir = Path(tempfile.mkdtemp())
    chunks: list[Path] = []
    transcripts: list[str] = []
    stop_event = threading.Event()
    chunk_queue: queue.Queue = queue.Queue()
    start_time = time.time()

    console.print(f"[bold green]🎤 Recording...[/] (say '{END_PHRASE}' to stop)\n")

    # Start background recording thread
    recorder = threading.Thread(
        target=_recorder_thread, args=(tmpdir, chunk_size, chunk_queue, stop_event), daemon=True
    )
    recorder.start()

    try:
        while not stop_event.is_set():
            # Check duration limit
            if duration and (time.time() - start_time) >= duration:
                break

            # Get next chunk (with timeout to allow checking stop condition)
            try:
                chunk_path = chunk_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            chunks.append(chunk_path)

            # Transcribe chunk (recording continues in background)
            try:
                text = transcribe_audio(chunk_path, model, language)
                if text:
                    transcripts.append(text)
                    console.print(f"[cyan]>[/] {text}")

                    if END_PHRASE in text.lower():
                        console.print("\n[yellow]End phrase detected, stopping...[/]")
                        break
            except Exception as e:
                console.print(f"[dim]Transcription error: {e}[/]")

    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped[/]")
    finally:
        stop_event.set()
        recorder.join(timeout=2)

    if not chunks:
        console.print("[yellow]No audio recorded[/]")
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise typer.Exit(1)

    # Final transcription of full audio for accuracy
    console.print("\n[dim]Finalizing transcription...[/]")
    merged_path = tmpdir / "merged.wav"
    merge_wav_files(chunks, merged_path)

    try:
        final_text = transcribe_audio(merged_path, model, language)
        # Remove end phrase from final text
        final_text = final_text.lower().replace(END_PHRASE, "").strip()
        final_text = " ".join(final_text.split())  # Clean whitespace
    except Exception:
        # Fallback to concatenated chunks
        final_text = " ".join(transcripts)
        if END_PHRASE in final_text.lower():
            final_text = final_text.lower().split(END_PHRASE)[0].strip()

    # Cleanup
    shutil.rmtree(tmpdir, ignore_errors=True)

    if not final_text:
        console.print("[yellow]No speech detected[/]")
        raise typer.Exit(1)

    console.print(Panel(final_text, title="Final Transcription", border_style="green"))

    if output:
        output.write_text(final_text)
        console.print(f"[dim]Saved to {output}[/]")

    if copy:
        try:
            if IS_MACOS:
                subprocess.run(["pbcopy"], input=final_text.encode(), check=True)
            else:
                subprocess.run(
                    ["xclip", "-selection", "clipboard"], input=final_text.encode(), check=True
                )
            console.print("[dim]Copied to clipboard[/]")
        except Exception:
            pass

    typer.echo(final_text)


def _record_simple(
    output: Optional[Path],
    model: str,
    duration: Optional[float],
    language: Optional[str],
    copy: bool,
):
    """Simple non-streaming record."""
    tmp = Path(tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name)

    console.print("[bold green]🎤 Recording...[/] (Ctrl+C to stop)")

    try:
        if duration:
            record_chunk(tmp, duration)
        else:
            # Record until Ctrl+C
            recorder = find_recorder()
            if recorder == "sox":
                cmd = ["rec", "-q", "-r", "16000", "-c", "1", "-b", "16", str(tmp)]
            elif IS_MACOS:
                device = get_default_audio_device()
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "avfoundation",
                    "-i",
                    device,
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    str(tmp),
                ]
            else:
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "alsa",
                    "-i",
                    "default",
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    str(tmp),
                ]

            proc = subprocess.Popen(cmd, stderr=subprocess.DEVNULL)
            try:
                proc.wait()
            except KeyboardInterrupt:
                proc.send_signal(signal.SIGINT)
                proc.wait()
    except KeyboardInterrupt:
        pass

    if not tmp.exists() or tmp.stat().st_size < 1000:
        tmp.unlink(missing_ok=True)
        console.print("[yellow]Recording too short[/]")
        raise typer.Exit(1)

    console.print(f"[dim]Recorded {tmp.stat().st_size // 1024}KB[/]")

    with console.status("[bold blue]Transcribing...[/]"):
        text = transcribe_audio(tmp, model, language)

    tmp.unlink(missing_ok=True)

    if not text:
        console.print("[yellow]No speech detected[/]")
        raise typer.Exit(1)

    console.print(Panel(text, title="Transcription", border_style="green"))

    if output:
        output.write_text(text)

    if copy:
        try:
            if IS_MACOS:
                subprocess.run(["pbcopy"], input=text.encode(), check=True)
            else:
                subprocess.run(
                    ["xclip", "-selection", "clipboard"], input=text.encode(), check=True
                )
        except Exception:
            pass

    typer.echo(text)


@app.command()
def file(
    path: Path = typer.Argument(..., help="Audio file to transcribe"),
    output: Optional[Path] = typer.Option(
        None, "-o", "--output", help="Save transcription to file"
    ),
    model: str = typer.Option(
        DEFAULT_MODEL, "-m", "--model", help="Model: tiny/base/small/medium/large/turbo"
    ),
    language: Optional[str] = typer.Option(None, "-l", "--language", help="Language code"),
):
    """Transcribe an audio file."""
    if not path.exists():
        console.print(f"[red]File not found: {path}[/]")
        raise typer.Exit(1)

    with console.status("[bold blue]Transcribing...[/]"):
        text = transcribe_audio(path, model, language)

    console.print(Panel(text, title="Transcription", border_style="green"))

    if output:
        output.write_text(text)
        console.print(f"[dim]Saved to {output}[/]")

    typer.echo(text)


@app.command()
def listen(
    model: str = typer.Option(
        DEFAULT_MODEL, "-m", "--model", help="Model: tiny/base/small/medium/large/turbo"
    ),
    language: Optional[str] = typer.Option(None, "-l", "--language", help="Language code"),
    prefix: str = typer.Option("", "-p", "--prefix", help="Prefix for each transcription"),
):
    """Continuous listening mode - press Enter to start each recording."""
    console.print("[bold]Continuous listening mode[/]")
    console.print(
        "[dim]Press Enter to record, say 'end recording' to stop each, Ctrl+C to exit[/]\n"
    )

    tmpdir = Path(tempfile.mkdtemp())
    chunk_size = 3.0

    try:
        while True:
            input("[Press Enter to record]")

            chunks = []
            transcripts = []
            chunk_num = 0

            console.print("[green]Recording...[/]")

            try:
                while True:
                    chunk_path = tmpdir / f"listen_{chunk_num:04d}.wav"
                    record_chunk(chunk_path, chunk_size)

                    if chunk_path.exists() and chunk_path.stat().st_size > 500:
                        chunks.append(chunk_path)
                        text = transcribe_audio(chunk_path, model, language)
                        if text:
                            transcripts.append(text)
                            console.print(f"  {text}")
                            if END_PHRASE in text.lower():
                                break
                    chunk_num += 1
            except KeyboardInterrupt:
                pass

            if transcripts:
                result = " ".join(transcripts)
                if END_PHRASE in result.lower():
                    result = result.lower().split(END_PHRASE)[0].strip()
                output = f"{prefix}{result}" if prefix else result
                console.print(f"[green]>[/green] {output}\n")

            # Cleanup chunks
            for c in chunks:
                c.unlink(missing_ok=True)

    except KeyboardInterrupt:
        console.print("\n[dim]Goodbye![/]")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.command()
def models():
    """List available Whisper models."""
    console.print("[bold]Available models:[/]\n")

    model_info = [
        ("tiny", "~75MB", "Fastest, least accurate"),
        ("base", "~140MB", "Fast, basic accuracy"),
        ("small", "~460MB", "Good balance"),
        ("medium", "~1.5GB", "High accuracy"),
        ("large", "~3GB", "Best accuracy"),
        ("turbo", "~1.6GB", "Best speed/accuracy (default)"),
    ]

    backend = "MLX" if IS_MACOS else "faster-whisper"
    console.print(f"[dim]Backend: {backend}[/]\n")

    for name, size, desc in model_info:
        mlx_name, fw_name = MODELS[name]
        actual = mlx_name if IS_MACOS else fw_name
        console.print(f"  [cyan]{name}[/] -> {actual}")
        console.print(f"    Size: {size} | {desc}\n")


if __name__ == "__main__":
    app()
