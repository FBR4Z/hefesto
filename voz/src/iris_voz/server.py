"""iris-voz: serviço de voz local da Íris.

Protocolo: um JSON por linha. Comandos chegam pelo stdin, eventos saem pelo stdout.

Comandos:
  {"cmd": "say", "text": str, "cache": bool}   enfileira uma fala
  {"cmd": "stop"}                              para de falar e limpa a fila
  {"cmd": "listen", "hint": str}               grava até o silêncio e transcreve
  {"cmd": "hint", "hint": str}                 dica para a próxima transcrição
  {"cmd": "cancel"}                            cancela a gravação em andamento
  {"cmd": "quit"}

Eventos:
  {"event": "ready", "tts": str, "stt": str, "wake": str}
  {"event": "speaking", "on": bool}
  {"event": "wake"}                            ouviu a palavra de ativação; a gravação começa
  {"event": "listening", "on": bool}
  {"event": "level", "value": float}           0..1, enquanto ouve ou fala
  {"event": "transcript", "text": str, "wake": bool}
  {"event": "error", "message": str}

Palavra de ativação (--wake): o Vosk, leve e no processador, vigia o microfone só
pela palavra "íris". Quando ela aparece, a gravação continua dali (com o áudio de
antes, para "Íris, abre o Gemini" dito de uma vez) e o Whisper transcreve; quem
chamou confere se a frase começa mesmo com "Íris".
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import queue
import site
import sys
import threading
import time
import wave
import zipfile
from collections import deque
from pathlib import Path

import numpy as np

MIC_RATE = 16_000
LEVEL_INTERVAL = 0.05
DEFAULT_MODELS = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "iris-voz" / "models"

RELEASE_URL = os.environ.get(
    "IRIS_VOZ_MODELOS_URL", "https://github.com/FBR4Z/iris/releases/download/modelos-voz-v1"
)
"""Mirror of every model in the Íris repository's releases (folders as .zip)."""

LOCAL_MODELS = os.environ.get("IRIS_VOZ_MODELOS_LOCAL", "")
"""Optional folder (pendrive, network share) holding the same files as the release."""

WHISPER_MODELS = {
    "base": "faster-whisper-base",
    "small": "faster-whisper-small",
    "large-v3-turbo": "faster-whisper-large-v3-turbo",
}
"""Whisper names → model folders (the release copy avoids the HuggingFace download)."""

MODEL_URLS = {
    "kokoro-v1.0.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
    "pt_BR-faber-medium.onnx": "https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR/faber/medium/pt_BR-faber-medium.onnx",
    "pt_BR-faber-medium.onnx.json": "https://huggingface.co/rhasspy/piper-voices/resolve/main/pt/pt_BR/faber/medium/pt_BR-faber-medium.onnx.json",
    "vosk-model-small-pt-0.3": "https://alphacephei.com/vosk/models/vosk-model-small-pt-0.3.zip",
}
"""Original sources, tried only when the local folder and the release fail."""

FOLDER_MODELS = {"vosk-model-small-pt-0.3", *WHISPER_MODELS.values()}
"""Models that are folders (shipped zipped, holding a folder with the model's name)."""

WAKE_MODEL = "vosk-model-small-pt-0.3"
WAKE_WORD = "íris"


def model_sources(name: str) -> list[str]:
    """Where to look for a model, in order: local folder, the Íris release, the original."""
    asset = f"{name}.zip" if name in FOLDER_MODELS else name
    sources = []
    if LOCAL_MODELS:
        sources.append(str(Path(LOCAL_MODELS) / asset))
    sources.append(f"{RELEASE_URL}/{asset}")
    if name in MODEL_URLS:
        sources.append(MODEL_URLS[name])
    return sources


def ensure_models(models: Path, names: list[str]) -> None:
    """Fetch any missing model files (or zipped model folders) into `models`."""
    import shutil
    import urllib.request

    models.mkdir(parents=True, exist_ok=True)
    for name in names:
        target = models / name
        if target.exists():
            continue
        partial = models / f"{name}.part"
        errors = []
        for source in model_sources(name):
            try:
                if "://" in source:
                    log(f"baixando {name} de {source}…")
                    urllib.request.urlretrieve(source, partial)
                elif Path(source).exists():
                    log(f"copiando {name} de {source}…")
                    shutil.copyfile(source, partial)
                else:
                    continue
                break
            except Exception as error:
                errors.append(f"{source}: {error}")
                partial.unlink(missing_ok=True)
        else:
            raise RuntimeError(f"não consegui obter {name}: " + "; ".join(errors))
        if name in FOLDER_MODELS:
            with zipfile.ZipFile(partial) as archive:
                archive.extractall(models)
            partial.unlink()
        else:
            partial.replace(target)


# --------------------------------------------------------------------------- io

_out_lock = threading.Lock()


def emit(event: str, **data) -> None:
    with _out_lock:
        sys.stdout.write(json.dumps({"event": event, **data}, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# ------------------------------------------------------------------------- cuda


def add_cuda_dlls() -> None:
    """On Windows the CUDA DLLs live inside the nvidia-* wheels; make them loadable."""
    for base in site.getsitepackages():
        for path in (Path(base) / "nvidia").glob("*/bin"):
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(str(path))
            os.environ["PATH"] = f"{path}{os.pathsep}{os.environ['PATH']}"


def has_cuda() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


# -------------------------------------------------------------------------- tts


class Speaker:
    """Text-to-speech with a phrase cache, played on a background thread."""

    def __init__(self, engine: str, voice: str, speed: float, models: Path, cuda: bool):
        self.models = models
        self.speed = speed
        self.cuda = cuda
        if engine == "auto":
            engine = "kokoro" if cuda else "piper"
        self.engine = engine
        self.voice = voice if engine == "kokoro" else "pt_BR-faber-medium"
        self._kokoro = None
        self._piper = None
        self._queue: queue.Queue[tuple[str, bool]] = queue.Queue()
        self._stop = threading.Event()
        self.playing = False
        self.stopped_at = 0.0
        """monotonic() when the last phrase ended, so the wake watcher skips the echo."""
        cache_root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "iris-voz" / "cache"
        self.cache_dir = cache_root / f"{self.engine}-{self.voice}-{self.speed}"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def description(self) -> str:
        device = "gpu" if (self.engine == "kokoro" and self.cuda) else "cpu"
        return f"{self.engine}/{self.voice}/{device}"

    def load(self) -> None:
        if self.engine == "kokoro":
            ensure_models(self.models, ["kokoro-v1.0.onnx", "voices-v1.0.bin"])
        else:
            ensure_models(self.models, [f"{self.voice}.onnx", f"{self.voice}.onnx.json"])
        if self.engine == "kokoro":
            if self.cuda:
                os.environ.setdefault("ONNX_PROVIDER", "CUDAExecutionProvider")
            from kokoro_onnx import Kokoro

            self._kokoro = Kokoro(
                str(self.models / "kokoro-v1.0.onnx"), str(self.models / "voices-v1.0.bin")
            )
            self._kokoro.create("ok", voice=self.voice, lang="pt-br")  # warm-up
        else:
            from piper import PiperVoice

            self._piper = PiperVoice.load(str(self.models / f"{self.voice}.onnx"))

    def synthesize(self, text: str, cache: bool) -> tuple[np.ndarray, int]:
        import soundfile as sf

        cache_path = self.cache_dir / (hashlib.sha1(text.encode()).hexdigest() + ".wav")
        if cache and cache_path.exists():
            samples, rate = sf.read(cache_path, dtype="float32")
            return samples, rate
        if self._kokoro is not None:
            samples, rate = self._kokoro.create(
                text, voice=self.voice, speed=self.speed, lang="pt-br"
            )
        else:
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as wav_file:
                self._piper.synthesize_wav(text, wav_file)
            buffer.seek(0)
            samples, rate = sf.read(buffer, dtype="float32")
        samples = np.asarray(samples, dtype=np.float32)
        if cache:
            sf.write(cache_path, samples, rate)
        return samples, rate

    def say(self, text: str, cache: bool) -> None:
        self._queue.put((text, cache))

    def stop(self) -> None:
        self._stop.set()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def run(self) -> None:
        import sounddevice as sd

        while True:
            text, cache = self._queue.get()
            self._stop.clear()
            try:
                samples, rate = self.synthesize(text, cache)
            except Exception as error:
                emit("error", message=f"TTS: {error}")
                continue
            if self._stop.is_set():
                continue
            self.playing = True
            emit("speaking", on=True)
            block = max(int(rate * LEVEL_INTERVAL), 256)
            try:
                with sd.OutputStream(samplerate=rate, channels=1, dtype="float32") as stream:
                    for start in range(0, len(samples), block):
                        if self._stop.is_set():
                            break
                        chunk = samples[start : start + block]
                        stream.write(chunk.reshape(-1, 1))
                        rms = float(np.sqrt(np.mean(chunk**2))) if len(chunk) else 0.0
                        emit("level", value=round(min(rms * 6, 1.0), 3))
            except Exception as error:
                emit("error", message=f"Áudio: {error}")
            emit("level", value=0.0)
            self.playing = False
            self.stopped_at = time.monotonic()
            emit("speaking", on=False)


# ------------------------------------------------------------------- microphone


class Microphone:
    """One input stream shared by whoever is listening (wake watcher, dictation).

    Each subscriber gets its own queue of 16 kHz mono float32 blocks. The stream is
    open only while someone is subscribed.
    """

    BLOCK = int(MIC_RATE * LEVEL_INTERVAL)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue[np.ndarray]] = []
        self._stream = None

    def subscribe(self) -> queue.Queue[np.ndarray]:
        import sounddevice as sd

        blocks: queue.Queue[np.ndarray] = queue.Queue()
        with self._lock:
            self._subscribers.append(blocks)
            if self._stream is None:
                self._stream = sd.InputStream(
                    samplerate=MIC_RATE,
                    channels=1,
                    dtype="float32",
                    blocksize=self.BLOCK,
                    callback=self._callback,
                )
                self._stream.start()
        return blocks

    def unsubscribe(self, blocks: queue.Queue[np.ndarray]) -> None:
        with self._lock:
            if blocks in self._subscribers:
                self._subscribers.remove(blocks)
            if not self._subscribers and self._stream is not None:
                stream, self._stream = self._stream, None
                stream.stop()
                stream.close()

    def _callback(self, indata, frames, time_info, status) -> None:
        block = indata[:, 0].copy()
        with self._lock:
            for blocks in self._subscribers:
                blocks.put(block)


# -------------------------------------------------------------------------- stt


class Listener:
    """Records until the speaker goes quiet, then transcribes with faster-whisper."""

    MAX_SECONDS = 20.0
    NO_SPEECH_SECONDS = 6.0
    END_SILENCE_SECONDS = 1.0
    WAKE_PAUSE_SECONDS = 3.0
    """After the wake word alone ("Íris…"), how long to wait for the rest."""

    def __init__(self, model: str, cuda: bool, microphone: Microphone, models: Path = DEFAULT_MODELS):
        if model == "auto":
            model = "large-v3-turbo" if cuda else "small"
        self.model_name = model
        self.models = models
        self.device = "cuda" if cuda else "cpu"
        self.compute = "float16" if cuda else "int8"
        self.microphone = microphone
        self._model = None
        self._cancel = threading.Event()
        self._busy = threading.Lock()
        self.hint = ""

    @property
    def busy(self) -> bool:
        return self._busy.locked()

    @property
    def description(self) -> str:
        return f"{self.model_name}/{self.device}"

    def load(self) -> None:
        from faster_whisper import WhisperModel

        model = self.model_name
        if folder := WHISPER_MODELS.get(model):
            try:
                ensure_models(self.models, [folder])
                model = str(self.models / folder)
            except Exception as error:
                # Last resort: let faster-whisper try HuggingFace itself.
                log(f"{error}; tentando o HuggingFace")
        self._model = WhisperModel(model, device=self.device, compute_type=self.compute)
        self._model.transcribe(np.zeros(MIC_RATE, dtype=np.float32), language="pt")

    def cancel(self) -> None:
        self._cancel.set()

    def listen(
        self,
        preroll: list[np.ndarray] | None = None,
        floor: float | None = None,
        blocks: queue.Queue[np.ndarray] | None = None,
    ) -> None:
        """Record and transcribe.

        With `preroll` (the wake word's audio) it carries on from there; `blocks` is a
        microphone subscription already taken, so nothing is lost in the hand-over.
        """
        if not self._busy.acquire(blocking=False):
            if blocks is not None:
                self.microphone.unsubscribe(blocks)
            return
        try:
            self._listen(preroll, floor, blocks)
        finally:
            self._busy.release()

    def _record(
        self,
        preroll: list[np.ndarray] | None,
        floor: float | None,
        blocks: queue.Queue[np.ndarray] | None,
    ) -> np.ndarray:
        frames: list[np.ndarray] = list(preroll or [])
        wake = preroll is not None
        # After the wake word the speaker is mid-sentence: no calibration, and a
        # longer pause is fine until they say the rest.
        speech_started = False
        silence = 0.0
        elapsed = 0.3 if wake else 0.0
        if blocks is None:
            blocks = self.microphone.subscribe()
        try:
            while not self._cancel.is_set():
                try:
                    chunk = blocks.get(timeout=1.0)
                except queue.Empty:
                    raise RuntimeError("o microfone parou de enviar áudio")
                frames.append(chunk)
                elapsed += LEVEL_INTERVAL
                rms = float(np.sqrt(np.mean(chunk**2)))
                emit("level", value=round(min(rms * 12, 1.0), 3))
                if elapsed <= 0.3:
                    # Calibrate the noise floor on the first moments.
                    floor = rms if floor is None else max(floor, rms)
                    continue
                threshold = max((floor or 0.0) * 2.5, 0.012)
                if rms > threshold:
                    speech_started = True
                    silence = 0.0
                else:
                    silence += LEVEL_INTERVAL
                if speech_started and silence >= self.END_SILENCE_SECONDS:
                    break
                if not speech_started:
                    if wake and silence >= self.WAKE_PAUSE_SECONDS:
                        break  # just "Íris": transcribe what we have
                    if not wake and elapsed >= self.NO_SPEECH_SECONDS:
                        return np.zeros(0, dtype=np.float32)
                if elapsed >= self.MAX_SECONDS:
                    break
        finally:
            self.microphone.unsubscribe(blocks)
        return np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)

    def _listen(
        self,
        preroll: list[np.ndarray] | None,
        floor: float | None,
        blocks: queue.Queue[np.ndarray] | None,
    ) -> None:
        wake = preroll is not None
        self._cancel.clear()
        emit("listening", on=True)
        try:
            audio = self._record(preroll, floor, blocks)
        except Exception as error:
            emit("error", message=f"Microfone: {error}")
            audio = np.zeros(0, dtype=np.float32)
        finally:
            emit("level", value=0.0)
            emit("listening", on=False)
        if self._cancel.is_set() or not len(audio):
            emit("transcript", text="", wake=wake)
            return
        if self._model is None:
            self.load()
        segments, _ = self._model.transcribe(
            audio,
            language="pt",
            beam_size=1,
            vad_filter=True,
            initial_prompt=self.hint or None,
        )
        text = " ".join(segment.text.strip() for segment in segments)
        # In iris-voz.log, to see what Whisper made of it when dictation misbehaves.
        try:
            print(f"[transcrição{' após ativação' if wake else ''}] {text!r}", file=sys.stderr, flush=True)
        except (UnicodeError, OSError):
            pass  # never lose the transcript over a log line
        emit("transcript", text=text, wake=wake)


# ------------------------------------------------------------------------- wake


class WakeWatcher:
    """Keeps an ear out for the wake word with Vosk, then hands over to the Listener.

    Vosk runs with a grammar of just the wake word (anything else is "[unk]"), so it
    is cheap enough to stay on all the time on the CPU. It pauses while Íris speaks
    (so she doesn't wake herself) and while dictation is recording.
    """

    PREROLL_SECONDS = 2.0
    ECHO_SECONDS = 0.6
    """Ignore the microphone this long after Íris stops talking."""

    def __init__(
        self,
        models: Path,
        microphone: Microphone,
        listener: Listener,
        speaker: Speaker | None,
        word: str = WAKE_WORD,
    ):
        self.models = models
        self.microphone = microphone
        self.listener = listener
        self.speaker = speaker
        self.word = word
        self._model = None
        self._stop = threading.Event()

    @property
    def description(self) -> str:
        return f"vosk/{self.word}"

    def load(self) -> None:
        ensure_models(self.models, [WAKE_MODEL])
        from vosk import Model, SetLogLevel

        SetLogLevel(-1)
        self._model = Model(str(self.models / WAKE_MODEL))
        find_word = getattr(self._model, "find_word", None)  # newer vosk only
        if find_word is not None and find_word(self.word) < 0:
            raise RuntimeError(f"o modelo {WAKE_MODEL} não conhece a palavra {self.word!r}")

    def _recognizer(self):
        from vosk import KaldiRecognizer

        grammar = json.dumps([self.word, "[unk]"], ensure_ascii=False)
        return KaldiRecognizer(self._model, MIC_RATE, grammar)

    def _paused(self) -> bool:
        if self.listener.busy:
            return True
        if self.speaker is not None:
            if self.speaker.playing:
                return True
            if time.monotonic() - self.speaker.stopped_at < self.ECHO_SECONDS:
                return True
        return False

    def heard(self, recognizer, block: np.ndarray) -> bool:
        """Feed one block; True when the wake word shows up."""
        pcm = (np.clip(block, -1, 1) * 32767).astype(np.int16).tobytes()
        if recognizer.AcceptWaveform(pcm):
            text = json.loads(recognizer.Result()).get("text", "")
        else:
            text = json.loads(recognizer.PartialResult()).get("partial", "")
        return self.word in text.split()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        keep = int(self.PREROLL_SECONDS / LEVEL_INTERVAL)
        preroll: deque[np.ndarray] = deque(maxlen=keep)
        levels: deque[float] = deque(maxlen=keep * 2)
        recognizer = self._recognizer()
        blocks = self.microphone.subscribe()
        try:
            while not self._stop.is_set():
                try:
                    block = blocks.get(timeout=1.0)
                except queue.Empty:
                    continue
                if self._paused():
                    # Start afresh afterwards: no stale audio, no half-heard word.
                    if preroll:
                        preroll.clear()
                        recognizer = self._recognizer()
                    continue
                preroll.append(block)
                levels.append(float(np.sqrt(np.mean(block**2))))
                if not self.heard(recognizer, block):
                    continue
                # A quiet-ish percentile of the recent level is the room's noise floor.
                floor = float(np.percentile(levels, 20)) if levels else None
                emit("wake")
                audio, preroll = list(preroll), deque(maxlen=keep)
                recognizer = self._recognizer()
                threading.Thread(
                    target=self.listener.listen,
                    args=(audio, floor, self.microphone.subscribe()),
                    daemon=True,
                    name="listener",
                ).start()
                # Let the listener take the busy lock before we look again.
                time.sleep(LEVEL_INTERVAL * 2)
        finally:
            self.microphone.unsubscribe(blocks)


# ------------------------------------------------------------------------- main


def machine_models(cuda: bool, stt: str, everything: bool) -> list[str]:
    """Models this machine uses: Kokoro + Whisper turbo on the GPU, Piper + small on the CPU."""
    gpu = ["kokoro-v1.0.onnx", "voices-v1.0.bin", WHISPER_MODELS["large-v3-turbo"]]
    cpu = ["pt_BR-faber-medium.onnx", "pt_BR-faber-medium.onnx.json", WHISPER_MODELS["small"]]
    if everything:
        return [*gpu, *cpu, WHISPER_MODELS["base"], WAKE_MODEL]
    names = gpu if cuda else cpu
    if stt in WHISPER_MODELS and WHISPER_MODELS[stt] not in names:
        names.append(WHISPER_MODELS[stt])
    return [*names, WAKE_MODEL]


def package_models(models: Path, destination: Path) -> None:
    """Copy the downloaded models to `destination` as release assets (folders zipped)."""
    import shutil

    destination.mkdir(parents=True, exist_ok=True)
    for name in machine_models(False, "", True):
        source = models / name
        if not source.exists():
            log(f"faltando {name} (rode 'iris-voz baixar --todos' antes)")
            continue
        if name in FOLDER_MODELS:
            target = destination / f"{name}.zip"
            # Stored, not deflated: the weights barely compress and zipping 1.6 GB is slow.
            with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as archive:
                for path in sorted(source.rglob("*")):
                    archive.write(path, path.relative_to(models).as_posix())
        else:
            target = destination / name
            shutil.copyfile(source, target)
        log(f"{target} ({target.stat().st_size / 2**20:.0f} MB)")


def main() -> None:
    parser = argparse.ArgumentParser(prog="iris-voz", description=__doc__.splitlines()[0])
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=["serve", "check", "baixar", "empacotar"],
        help="baixar: busca os modelos desta máquina (ou --todos) e sai; "
        "empacotar PASTA: copia os modelos baixados para PASTA no formato da release",
    )
    parser.add_argument("pasta", nargs="?", help="destino do empacotar")
    parser.add_argument("--de", help="pasta local com os modelos (pendrive, rede), antes da internet")
    parser.add_argument("--todos", action="store_true", help="baixar: todos os modelos (CPU e GPU)")
    parser.add_argument("--tts", default="auto", choices=["auto", "kokoro", "piper", "off"])
    parser.add_argument("--stt", default="off", help="auto, off ou nome do modelo Whisper")
    parser.add_argument("--voice", default="pf_dora", help="voz do Kokoro")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--cpu", action="store_true", help="ignora a GPU")
    parser.add_argument(
        "--wake", action="store_true", help='vigia a palavra de ativação (precisa de --stt)'
    )
    parser.add_argument("--wake-word", default=WAKE_WORD, help='palavra de ativação (padrão: "íris")')
    parser.add_argument("--models", default=os.environ.get("IRIS_VOZ_MODELS", str(DEFAULT_MODELS)))
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    add_cuda_dlls()
    cuda = has_cuda() and not args.cpu

    models = Path(args.models)
    if args.de:
        global LOCAL_MODELS
        LOCAL_MODELS = args.de
    if args.command == "baixar":
        names = machine_models(cuda, args.stt, args.todos)
        ensure_models(models, names)
        log(f"modelos prontos em {models}: {', '.join(names)}")
        return
    if args.command == "empacotar":
        if not args.pasta:
            parser.error("empacotar precisa da pasta de destino")
        package_models(models, Path(args.pasta))
        return
    microphone = Microphone()
    speaker = None if args.tts == "off" else Speaker(args.tts, args.voice, args.speed, models, cuda)
    listener = None if args.stt == "off" else Listener(args.stt, cuda, microphone, models)
    watcher = None
    if args.wake and listener is not None:
        watcher = WakeWatcher(models, microphone, listener, speaker, args.wake_word.lower())

    try:
        if speaker is not None:
            speaker.load()
        if listener is not None:
            listener.load()
    except Exception as error:
        emit("error", message=f"Falha ao carregar modelos: {error}")
        sys.exit(1)
    if watcher is not None:
        # Without the wake word, dictation (F9) still works.
        try:
            watcher.load()
        except Exception as error:
            emit("error", message=f"Palavra de ativação indisponível: {error}")
            watcher = None

    emit(
        "ready",
        tts=speaker.description if speaker else "off",
        stt=listener.description if listener else "off",
        wake=watcher.description if watcher else "off",
    )
    if args.command == "check":
        if speaker is not None:
            speaker.say("Voz da Íris funcionando.", cache=False)
            threading.Thread(target=speaker.run, daemon=True).start()
            time.sleep(4)
        return

    if speaker is not None:
        threading.Thread(target=speaker.run, daemon=True, name="speaker").start()
    if watcher is not None:
        threading.Thread(target=watcher.run, daemon=True, name="wake").start()

    for line in sys.stdin:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        command = message.get("cmd")
        if command == "say" and speaker is not None:
            speaker.say(str(message.get("text", "")), bool(message.get("cache", False)))
        elif command == "stop" and speaker is not None:
            speaker.stop()
        elif command == "listen" and listener is not None:
            if speaker is not None:
                speaker.stop()  # don't transcribe ourselves
            listener.hint = str(message.get("hint", ""))
            threading.Thread(target=listener.listen, daemon=True, name="listener").start()
        elif command == "hint" and listener is not None:
            listener.hint = str(message.get("hint", ""))
        elif command == "cancel" and listener is not None:
            listener.cancel()
        elif command == "quit":
            break
    if watcher is not None:
        watcher.stop()


if __name__ == "__main__":
    main()
