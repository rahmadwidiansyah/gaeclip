from __future__ import annotations

from pathlib import Path

from .errors import CancelledError, ExternalCommandError
from .subtitles import Cue, normalize_cues


def _ensure_av_compat() -> None:
    """Patch av.open agar tahan terhadap perbedaan versi PyAV.

    faster-whisper>=1.1 memanggil:
        av.open(path, mode="r", metadata_errors="ignore")
    sedangkan image worker saat ini ter-install `av==19.0.0`
    (fork WyattBlue) yang signature-nya cuma
        open(file, mode='r', **kwargs tanpa metadata_errors)
    sehingga meledak:
        TypeError: open() got an unexpected keyword argument 'metadata_errors'

    Shim ini membuang kwarg yang tidak didukung, jadi transcribe jalan
    di av 12.x/14.x (PyAV-Org) maupun 15.x-19.x (fork).
    Idempotent dan tanpa efek samping kalau versi sudah cocok.
    """
    try:
        import av
    except ImportError:
        return
    if getattr(av.open, "_gaeclip_compat_patched", False):
        return
    orig_open = av.open

    def _compat_open(*args, **kwargs):  # type: ignore[no-untyped-def]
        # Hanya buang kwarg yang memang tidak dikenal C-extension.
        # Caranya aman: coba panggil original, kalau TypeError soal
        # metadata_errors, ulangi tanpa kwarg tersebut.
        try:
            return orig_open(*args, **kwargs)
        except TypeError as exc:
            if "metadata_errors" in str(exc) and "metadata_errors" in kwargs:
                kwargs.pop("metadata_errors", None)
                return orig_open(*args, **kwargs)
            raise

    _compat_open._gaeclip_compat_patched = True  # type: ignore[attr-defined]
    av.open = _compat_open  # type: ignore[assignment]


def transcribe_with_whisper(
    source: Path,
    model_name: str = "small",
    language: str = "id",
    should_cancel=None,
    cancel_check_every: int = 25,
    on_progress=None,
    progress_every: int = 10,
    total_duration: float | None = None,
):
    """Transcribe `source` with faster-whisper, streaming segments.

    `on_progress(processed_seconds, total_seconds)` dipanggil tiap
    `progress_every` segmen + sekali di akhir agar UI tidak kelihatan
    stuck di 35% saat model medium/large mengunyah audio 1 jam di CPU.
    `total_seconds` 0.0 berarti tidak diketahui (info None + tanpa
    total_duration) — caller boleh pakai durasi metadata sebagai fallback.
    CancelledError dari `on_progress` (misal worker mendeteksi job
    dibatalkan) tidak ditelan dan langsung dibiarkan naik.
    """
    _ensure_av_compat()
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ExternalCommandError(
            "No subtitles found and faster-whisper is not installed; "
            "install gaeclip[transcription]"
        ) from exc
    model = WhisperModel(model_name, device="auto", compute_type="int8")
    segments, info = model.transcribe(str(source), language=language, vad_filter=True)
    if total_duration is None:
        try:
            total_duration = float(getattr(info, "duration", 0) or 0) or None
        except (TypeError, ValueError):
            total_duration = None
    cues = []
    last_end = 0.0
    # Segments stream in order: poll cancellation between chunks so a
    # cancelled hour-long transcription stops within seconds, not at the end.
    for index, segment in enumerate(segments):
        if (should_cancel is not None and index % max(1, cancel_check_every) == 0
                and should_cancel()):
            raise CancelledError("Transcription cancelled by user")
        text = segment.text.strip()
        if text and segment.end > segment.start:
            cues.append(Cue(float(segment.start), float(segment.end), text, index))
        try:
            last_end = float(segment.end)
        except (TypeError, ValueError):
            pass
        if (on_progress is not None and progress_every > 0
                and index % max(1, progress_every) == 0):
            try:
                on_progress(last_end, float(total_duration) if total_duration else 0.0)
            except CancelledError:
                raise
            except Exception:
                pass
    if on_progress is not None:
        try:
            done_total = float(total_duration) if total_duration else last_end
            on_progress(last_end or done_total, done_total)
        except CancelledError:
            raise
        except Exception:
            pass
    if not cues:
        raise ExternalCommandError("faster-whisper produced no transcript segments")
    return normalize_cues(cues, source="faster_whisper", language=language)
