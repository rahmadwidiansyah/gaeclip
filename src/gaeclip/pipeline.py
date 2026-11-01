from __future__ import annotations

import logging
from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

from .config import Settings
from .curation import CurationResult, GeminiCurator, curate_with_fallback
from .downloader import download_audio, download_subtitles, download_video_section, video_metadata
from .errors import CancelledError, ValidationError
from .media import render_static_vertical, subtitle_chars_per_line
from .storage import JobStorage, new_job_id
from .subtitles import normalize_max_words_per_line, parse_srt, parse_vtt, write_ass
from .transcription import transcribe_with_whisper
from .visual import FaceObservation, build_speaker_timeline, make_layout_instructions


@dataclass(frozen=True)
class GeneratedClip:
    candidate_id: str
    path: Path
    title: str = ""
    score: float = 0.0
    reason: str = ""
    # Transcript span (seconds) backing this clip; persisted into the job
    # record so later features (e.g. social metadata) can slice context
    # without re-deriving timing.
    start: float = 0.0
    end: float = 0.0
    # Publication metadata generated during the pipeline run (spec: clip
    # leaves creation with description+hashtags ready). Empty when the clip
    # predates the feature or generation failed; the Publish form then
    # falls back to manual generation.
    description: str = ""
    hashtags: tuple = ()
    metadata_source: str = ""
    metadata_updated_at: float | None = None


@dataclass
class ClipTask:
    """Per-clip roadmap record: survives partial failures for the UI."""

    clip_id: str
    title: str = ""
    status: str = "pending"  # pending|downloading|rendering|verifying|done|failed
    stage_detail: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    score: float = 0.0
    reason: str = ""
    duration_seconds: float | None = None
    size_bytes: int | None = None
    attempt: int = 1
    framing_mode: str = "static"
    framing_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "clip_id": self.clip_id,
            "title": self.title,
            "status": self.status,
            "stage_detail": self.stage_detail,
            "error": (
                {"code": self.error_code, "message": self.error_message}
                if self.error_message
                else None
            ),
            "score": self.score,
            "reason": self.reason,
            "duration_seconds": self.duration_seconds,
            "size_bytes": self.size_bytes,
            "attempt": self.attempt,
            "framing_mode": self.framing_mode,
            "framing_reason": self.framing_reason,
        }


@dataclass(frozen=True)
class GenerationResult:
    job_id: str
    source: Path
    curation: CurationResult
    clips: list[GeneratedClip]
    clip_tasks: list[ClipTask] | None = None
    # Transcript provenance for the job timeline (cache visibility).
    transcript_reused: bool = False
    transcript_cache_id: str | None = None
    transcript_engine: str = ""  # youtube_auto | faster_whisper


def _log_framing_qc(output: Path, clip_id: str, job_name: str,
                    expected_duration: float, instructions: list,
                    plan, debug_framing: bool, source=None,
                    expected_fps: float | None = None) -> None:
    """Post-render QC + debug dump. Strictly log-only: any failure here is
    swallowed so QC can never break a successfully rendered clip."""
    try:
        from .framing_qc import dump_debug_artifacts, qc_clip
        from .media import probe_dimensions

        trajectory = list(getattr(plan, "trajectory", []) or [])
        src_w, src_h = None, None
        exp_w, exp_h = 1080, 1920
        if source is not None:
            try:
                probed = probe_dimensions(source)
                if probed is not None:
                    src_w, src_h = probed
                from .media import resolve_output_geometry

                exp_w, exp_h = resolve_output_geometry(source)
            except Exception:
                pass
        report = qc_clip(
            output, clip_id, expected_duration,
            exp_w=exp_w, exp_h=exp_h, exp_fps=expected_fps,
            instructions=instructions, trajectory=trajectory or None,
            src_w=src_w, src_h=src_h,
        )
        logger.info("Job %s clip %s framing QC:\n%s",
                    job_name, clip_id, report.summary())
        final = report.metrics.get("final_media") or {}
        if final:
            logger.info(
                "[FINAL_MEDIA] width=%s height=%s fps=%s sar=%s dar=%s "
                "codec=%s pixel_format=%s audio_codec=%s duration=%s",
                final.get("width"), final.get("height"), final.get("fps"),
                final.get("sar"), final.get("dar"), final.get("codec"),
                final.get("pixel_format"), final.get("audio_codec"),
                final.get("duration"),
            )
        if debug_framing and plan is not None:
            for written in dump_debug_artifacts(output, plan):
                logger.info("Job %s clip %s framing debug artifact: %s",
                            job_name, clip_id, written)
    except Exception as exc:
        logger.warning("Job %s clip %s framing QC skipped: %s",
                       job_name, clip_id, exc)


def _clip_metadata_best_effort(*, lines, candidate, metadata, language,
                                duration_seconds, client, settings, ai_chain,
                                job_id, index, total) -> dict:
    """Generate publication metadata for one finished clip (never raises).

    Runs inline after render using in-memory context only: the candidate's
    logical span slices the transcript, the raw yt-dlp metadata supplies
    source title/channel. Any metadata problem (including a broken local
    fallback) yields ``source="failed"`` with empty fields — the clip and
    job still complete, and Publish falls back to manual generation.
    """
    from .clip_metadata import (
        ClipMetadataContext,
        generate_clip_metadata_safe,
        slice_span_text,
    )

    span_text, span_lang = slice_span_text(lines, candidate.start, candidate.end)
    source_title = str(metadata.get("title") or metadata.get("fulltitle") or "")
    source_channel = str(metadata.get("channel") or metadata.get("uploader") or "")
    ctx = ClipMetadataContext(
        job_id=job_id, clip_id=candidate.candidate_id,
        source_title=source_title, source_channel=source_channel,
        clip_title=candidate.title, clip_reason=candidate.reason,
        clip_duration=duration_seconds,
        language=span_lang or language or "id",
        span_text=span_text,
    )
    logger.info("[CLIP_METADATA] generating job=%s clip=%s (%d/%d)",
                job_id, candidate.candidate_id, index + 1, total)
    try:
        return generate_clip_metadata_safe(
            ctx, client=client, settings=settings, ai_chain=ai_chain)
    except Exception as exc:
        # generate_clip_metadata_safe only lets CancelledError through, but
        # keep this frame total-safe: metadata must never fail a clip.
        from .errors import CancelledError as _Cancelled

        if isinstance(exc, _Cancelled):
            raise
        import time as _time

        logger.info("[CLIP_METADATA] failed job=%s clip=%s (%s); storing empty package",
                    job_id, candidate.candidate_id, type(exc).__name__)
        return {
            "description": "", "hashtags": [], "source": "failed",
            "provider": None, "model": None, "updated_at": _time.time(),
        }


def _build_pool_ctx(redis_client, ai_chain, job_id):
    """Assemble the Gemini provider pool context for one job.

    Any failure here returns None (legacy single-key path), never bricks
    curation: pool construction must not fail the job.
    """
    if redis_client is None:
        return None
    try:
        from .gemini_pool import build_pool_context

        return build_pool_context(redis_client, ai_chain, job_id)
    except Exception:
        logger.warning("Gemini pool context build failed; using legacy chain", exc_info=True)
        return None


def generate_from_url(
    url: str,
    settings: Settings | None = None,
    language: str = "id",
    yt_dlp: str = "yt-dlp",
    observations: list[FaceObservation] | None = None,
    progress: Callable[[str, int], None] | None = None,
    job_id: str | None = None,
    max_clips: int | None = None,
    min_duration: float | None = None,
    max_duration: float | None = None,
    burn_subtitles: bool = True,
    on_clip_task: Callable[[list[ClipTask]], None] | None = None,
    on_metadata: Callable[[dict], None] | None = None,
    auto_framing: bool = True,
    debug_framing: bool = False,
    subtitle_style: str = "casual_center",
    subtitle_position: str = "bottom_center",
    subtitle_max_words_per_line: int | None = None,
    branding: dict | None = None,
    ai_chain: list[str] | None = None,
    redis_client=None,
    adopt_transcript_job_id: str | None = None,
    force_retranscribe: bool = False,
) -> GenerationResult:
    settings = settings or Settings.from_env()
    eff_max_clips = max_clips if max_clips is not None else settings.max_clips
    eff_min_duration = min_duration if min_duration is not None else settings.min_clip_duration_seconds
    eff_max_duration = max_duration if max_duration is not None else settings.max_clip_duration_seconds
    if eff_min_duration > eff_max_duration:
        raise ValidationError("Minimum clip duration exceeds maximum")
    if not 1 <= eff_max_clips <= 10:
        raise ValidationError("max_clips must be between 1 and 10")
    settings.ensure_storage()
    storage = JobStorage(settings.storage_root, job_id or new_job_id())
    job_root = storage.create()

    def should_cancel() -> bool:
        """True when the job was cancelled or deleted mid-run.

        Built from redis_client when available so long blocking stages
        (transcription) and the per-clip loop abort promptly instead of
        burning a worker slot on a dead job.
        """
        if redis_client is None or job_id is None:
            return False
        try:
            from .jobs import JobRepository

            live = JobRepository(redis_client).get(job_id)
        except Exception:
            return False
        return live is None or live.status == "cancelled"
    source_dir = job_root / "source"
    if progress:
        progress("metadata", 15)
    metadata = video_metadata(url, yt_dlp=yt_dlp)
    if on_metadata is not None:
        try:
            on_metadata(metadata)
        except Exception:
            pass
    duration = metadata.get("duration")
    duration = float(duration) if duration is not None else None
    if duration is not None and duration > settings.max_source_duration_seconds:
        raise ValidationError("Source video exceeds configured duration limit")
    if progress:
        progress("subtitles", 25)
    # Transcript source menolak mencampur detik audio ke counter clip:
    # progress transkripsi memakai current=0/total=0 (detik hanya di
    # stage_detail) agar WorkflowGraph tidak merender ribuan node clip.
    # `burn_subtitles` hanya mengontrol burn-in ke output, bukan pencarian.
    transcript_source = getattr(settings, "transcript_source", "youtube_first") or "youtube_first"
    if transcript_source not in {"youtube_first", "whisper_only", "youtube_only"}:
        raise ValidationError("TRANSCRIPT_SOURCE tidak dikenal")

    def _parse_subtitle_file(subtitle_path) -> list:
        subtitle_text = subtitle_path.read_text(encoding="utf-8")
        if len(subtitle_text) > settings.max_transcript_chars:
            raise ValidationError("Transcript exceeds configured size limit")
        parser = parse_vtt if subtitle_path.suffix.lower() == ".vtt" else parse_srt
        return parser(subtitle_text, source="youtube_auto", language=language)

    def _mmss(seconds: float) -> str:
        total = max(0, int(seconds))
        return f"{total // 60:02d}:{total % 60:02d}"

    def _on_transcribe(processed: float, total: float) -> None:
        """Petakan detik audio yang sudah ditranskrip ke 35..48%.

        Medium di CPU bisa 30 menit untuk video 1 jam; tanpa ini UI
        diam di 35% dan dikira stuck. Detik audio HANYA di detail
        string — current/total clip tetap 0 agar tidak mencemari
        WorkflowGraph (regresi 4000 node).
        """
        if not progress:
            return
        eff_total = total or duration or 0
        if eff_total and eff_total > 0:
            frac = min(1.0, max(0.0, processed / eff_total))
        else:
            frac = 0.0
        pct = 35 + int(round(13 * frac))
        if eff_total and eff_total > 0:
            detail = (
                f"Mentranskrip {settings.whisper_model} "
                f"{_mmss(processed)}/{_mmss(eff_total)} "
                f"({int(frac * 100)}%)"
            )
        else:
            detail = f"Mentranskrip {settings.whisper_model} {_mmss(processed)}"
        try:
            progress("transcribing", pct, detail, 0, 0)  # type: ignore[call-arg]
        except TypeError:
            progress("transcribing", pct)

    def _transcript_cache_or_none():
        """Shared Whisper cache; None without Redis (CLI/offline) so the
        pipeline still works — cache is an optimization, never required."""
        if redis_client is None:
            return None
        try:
            from .transcript_cache import TranscriptCache  # noqa: PLC0415

            return TranscriptCache(redis_client, settings.storage_root)
        except Exception as exc:
            logger.warning("[TRANSCRIPT_CACHE] unavailable, continuing without cache: %s", exc)
            return None

    def _emit_simple(stage: str, pct: int, detail: str | None = None) -> None:
        if not progress:
            return
        try:
            progress(stage, pct, detail, 0, 0)  # type: ignore[call-arg]
        except TypeError:
            try:
                progress(stage, pct)
            except Exception:
                pass
        except Exception:
            pass

    def _save_job_transcript(
        dest: Path, engine: str, whisper_model: str, lang: str,
        transcript_lines: list,
    ) -> None:
        """Best-effort per-job transcript.json (adopsi Recreate + debug).

        Tidak pernah menggagalkan job bila disk bermasalah.
        """
        try:
            import json as _json
            import time as _time

            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(
                _json.dumps(
                    {
                        "schema_version": 1,
                        "engine": engine,
                        "whisper_model": whisper_model,
                        "language": lang,
                        "created_at": _time.time(),
                        "line_count": len(transcript_lines),
                        "lines": [
                            line.model_dump(mode="json") for line in transcript_lines
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Job %s: transcript.json save skipped: %s", storage.job_root.name, exc)

    def _try_adopt_parent_transcript(cache) -> tuple[list | None, Any]:
        """Adopsi transcript.json job lama (pra-cache) ke shared cache.

        Hanya baris faster_whisper dengan model+bahasa yang cocok yang
        diadopsi — subtitle YT tidak dimasukkan ke Whisper cache.
        """
        if not adopt_transcript_job_id or cache is None:
            return None, None
        try:
            from .jobs import JobRepository  # noqa: PLC0415
            from .storage import JobStorage as _JobStorage  # noqa: PLC0415

            parent_storage = _JobStorage(settings.storage_root, adopt_transcript_job_id)
            candidate = parent_storage.job_root / "source" / "transcript.json"
            if not candidate.is_file():
                return None, None
            import json as _json

            payload = _json.loads(candidate.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("engine") != "faster_whisper":
                return None, None
            if payload.get("whisper_model") != settings.whisper_model:
                return None, None
            if payload.get("language") != language:
                return None, None
            lines, record = cache.adopt_job_transcript(
                candidate, url, settings.whisper_model, language, duration,
            )
            if lines:
                logger.info(
                    "[TRANSCRIPT_CACHE] reuse adopted parent=%s cache_id=%s lines=%d",
                    adopt_transcript_job_id, record.cache_id if record else "-", len(lines),
                )
            return lines, record
        except Exception as exc:
            logger.warning("[TRANSCRIPT_CACHE] adopt failed, continuing fresh: %s", exc)
            return None, None

    def _run_whisper_with_cache(cache) -> tuple[list, bool, str | None]:
        """Returns (lines, reused, cache_id). Never runs Whisper twice for
        one (source, config): lock + wait deduplicates concurrent jobs."""
        from .transcript_cache import (  # noqa: PLC0415
            source_key_for,
            transcription_config_hash,
        )
        from .url import extract_video_id as _extract_vid  # noqa: PLC0415

        vid = _extract_vid(url)
        canonical = f"https://www.youtube.com/watch?v={vid}" if vid else url.strip()
        source_key, _ = source_key_for(vid, canonical)
        config_hash = transcription_config_hash(settings.whisper_model, language)
        owner = storage.job_root.name

        if cache is not None and not force_retranscribe:
            lines, record, outcome = cache.lookup(url, settings.whisper_model, language, duration)
            if outcome == "hit" and lines:
                _emit_simple(
                    "transcribing", 48,
                    "Transkripsi dilewati — menggunakan hasil tersimpan",
                )
                logger.info(
                    "[TRANSCRIPT_CACHE] reuse lines=%d cache_id=%s job=%s",
                    len(lines), record.cache_id if record else "-", owner,
                )
                return lines, True, record.cache_id if record else None
            adopted, adopted_record = _try_adopt_parent_transcript(cache)
            if adopted:
                _emit_simple(
                    "transcribing", 48,
                    "Transkripsi dilewati — memakai transkrip job sebelumnya",
                )
                return adopted, True, adopted_record.cache_id if adopted_record else None
            if cache.acquire_lock(source_key, config_hash, owner):
                locked = True
            else:
                logger.info(
                    "[TRANSCRIPT_CACHE] lock held source=%s, waiting for peer job", source_key,
                )
                waited, waited_record = cache.wait_for_transcript(
                    url, settings.whisper_model, language, duration,
                    progress=progress, should_cancel=should_cancel,
                )
                if waited:
                    logger.info("[TRANSCRIPT_CACHE] reuse lines=%d after wait", len(waited))
                    _emit_simple(
                        "transcribing", 48,
                        "Transkripsi dilewati — menggunakan hasil tersimpan",
                    )
                    return waited, True, waited_record.cache_id if waited_record else None
                if cache.acquire_lock(source_key, config_hash, owner):
                    locked = True
                else:
                    locked = False
        else:
            locked = False

        try:
            if progress:
                try:
                    progress("transcribing", 35, "Mengunduh audio untuk transkripsi", 0, 0)  # type: ignore[call-arg]
                except TypeError:
                    progress("transcribing", 35)
            audio = download_audio(url, source_dir, yt_dlp=yt_dlp)
            lines = transcribe_with_whisper(audio, model_name=settings.whisper_model, language=language,
                                            should_cancel=should_cancel,
                                            on_progress=_on_transcribe if progress else None,
                                            total_duration=duration)
        finally:
            if cache is not None and locked:
                cache.release_lock(source_key, config_hash, owner)
        if cache is not None:
            stored = cache.store(url, settings.whisper_model, language, lines, duration)
            return lines, False, stored.cache_id if stored else None
        return lines, False, None

    if transcript_source == "whisper_only":
        _emit_simple("subtitles", 25, "Melewati subtitle YouTube (mode whisper_only)")
        cache = _transcript_cache_or_none()
        lines, transcript_reused, transcript_cache_id = _run_whisper_with_cache(cache)
        transcript_engine = "faster_whisper"
    elif transcript_source == "youtube_only":
        subtitle_path = download_subtitles(url, source_dir, language=language, yt_dlp=yt_dlp)
        if subtitle_path is None:
            raise ValidationError(
                "Video tidak punya subtitle YouTube (mode youtube_only). "
                "Ganti Sumber Transkrip ke youtube_first atau whisper_only."
            )
        lines = _parse_subtitle_file(subtitle_path)
        transcript_reused, transcript_cache_id, transcript_engine = False, None, "youtube_auto"
    else:  # youtube_first: perilaku lama (hemat CPU device kentang)
        subtitle_path = download_subtitles(url, source_dir, language=language, yt_dlp=yt_dlp)
        if subtitle_path is None:
            cache = _transcript_cache_or_none()
            lines, transcript_reused, transcript_cache_id = _run_whisper_with_cache(cache)
            transcript_engine = "faster_whisper"
        else:
            lines = _parse_subtitle_file(subtitle_path)
            transcript_reused, transcript_cache_id, transcript_engine = False, None, "youtube_auto"
    _save_job_transcript(
        source_dir / "transcript.json", transcript_engine,
        settings.whisper_model, language, lines,
    )
    if not lines:
        raise ValidationError("Subtitle file contains no usable cues")
    if progress:
        progress("curating", 50)
    # Pool-first gating: the multi-provider pool is usable even when the
    # legacy single GEMINI_API_KEY is empty (pool-only setups). Without this,
    # 5 configured providers would sit idle while jobs silently skip AI.
    pool_ctx = _build_pool_ctx(redis_client, ai_chain, job_id)
    pool_usable = False
    if pool_ctx is not None:
        try:
            pool_usable = any(
                provider.enabled and provider.api_key.strip() and provider.ordered_models()
                for provider in pool_ctx.providers
            )
        except Exception:
            pool_usable = False
    use_ai = bool(settings.gemini_api_key) or pool_usable
    skipped_reason = None
    skip_kind = None
    if not (bool(settings.gemini_api_key) or pool_usable):
        skipped_reason = "AI tidak dicoba (GEMINI_API_KEY kosong dan tidak ada provider pool)"
        skip_kind = "gemini_api_key_missing"
    # NOTE: long transcripts no longer skip AI entirely (curation-v2):
    # GeminiCurator chunks them into overlapping windows instead.
    curation = curate_with_fallback(
        lines,
        curator=(
            GeminiCurator(
                settings.gemini_api_key,
                model=(ai_chain[0] if ai_chain else settings.gemini_model),
                timeout_seconds=settings.gemini_timeout_seconds,
                min_duration=eff_min_duration,
                max_duration=eff_max_duration,
                max_clips=eff_max_clips,
                fallbacks=(ai_chain[1:] if ai_chain else None),
                pool_ctx=pool_ctx,
                input_char_limit=settings.max_llm_input_chars,
            )
            if use_ai
            else None
        ),
        min_duration=eff_min_duration,
        max_duration=eff_max_duration,
        max_clips=eff_max_clips,
        heatmap=metadata.get("heatmap"),
        skipped_reason=skipped_reason,
        skip_kind=skip_kind,
    )
    clips: list[GeneratedClip] = []
    output_dir = job_root / "clips"
    total = len(curation.candidates)
    if progress:
        try:
            progress("curating", 52, f"{total} momen terpilih", 0, total)  # type: ignore[call-arg]
        except TypeError:
            progress("curating", 52)

    def _emit(stage: str, pct: int, detail: str | None = None, current: int = 0, total_clips: int = 0) -> None:
        if not progress:
            return
        try:
            progress(stage, pct, detail, current, total_clips)  # type: ignore[call-arg]
        except TypeError:
            progress(stage, pct)

    def _sync_tasks() -> None:
        if on_clip_task is not None:
            on_clip_task(clip_tasks)

    clip_tasks: list[ClipTask] = []
    for index, candidate in enumerate(curation.candidates):
        if should_cancel():
            raise CancelledError("Job dibatalkan pengguna")
        task = ClipTask(
            clip_id=candidate.candidate_id, title=candidate.title,
            score=candidate.score, reason=candidate.reason,
        )
        # progress 55..95 spread across clips so the bar never looks stuck
        dl_progress = 55 + int(20 * index / max(1, total))
        _emit("downloading", dl_progress, f"Mengunduh clip {index + 1}/{total}", index, total)
        try:
            start = max(0, candidate.start - settings.clip_buffer_seconds)
            end = candidate.end + settings.clip_buffer_seconds
            if duration is not None:
                end = min(end, duration)
            if not end > start:
                raise ValidationError(
                    f"Candidate {candidate.candidate_id} is empty after clamping to source duration"
                )
            task.status = "downloading"
            task.stage_detail = f"Mengunduh clip {index + 1}/{total}"
            clip_tasks.append(task)
            _sync_tasks()
            source = download_video_section(
                url, source_dir, start, end, candidate.candidate_id, yt_dlp=yt_dlp,
            )
            subtitle_file: Path | None = None
            if burn_subtitles:
                from .media import resolve_output_geometry
                from .subtitle_style import resolve_subtitle_config

                try:
                    out_w, out_h = resolve_output_geometry(source)
                    style_cfg = resolve_subtitle_config(
                        subtitle_style, subtitle_position, out_w, out_h,
                    )
                except ValueError as exc:
                    raise ValidationError(str(exc)) from exc
                subtitle_file = output_dir / f"{candidate.candidate_id}.ass"
                output_dir.mkdir(parents=True, exist_ok=True)
                write_ass(
                    lines, subtitle_file, offset=start, clip_duration=end - start,
                    max_chars=subtitle_chars_per_line(out_w, out_h),
                    width=out_w, height=out_h,
                    style=subtitle_style, position=subtitle_position,
                    max_words=normalize_max_words_per_line(subtitle_max_words_per_line),
                )
                logger.info(
                    "Job %s clip %s subtitle: style=%s position=%s format=ass "
                    "font=%s size=%d alignment=%d video=%dx%d max_words=%d",
                    storage.job_root.name, candidate.candidate_id,
                    style_cfg.style, style_cfg.position, style_cfg.font_name,
                    style_cfg.font_size, style_cfg.alignment, out_w, out_h,
                    normalize_max_words_per_line(subtitle_max_words_per_line),
                )
            else:
                output_dir.mkdir(parents=True, exist_ok=True)
            output = output_dir / f"{candidate.candidate_id}.mp4"
            render_progress = 75 + int(20 * index / max(1, total))
            render_detail = (
                f"Merender clip {index + 1}/{total} (9:16 + subtitle)"
                if burn_subtitles
                else f"Merender clip {index + 1}/{total} (9:16 tanpa subtitle)"
            )
            _emit("rendering", render_progress, render_detail, index + 1, total)
            task.status = "rendering"
            task.stage_detail = render_detail
            _sync_tasks()
            framing_mode = "static"
            framing_reason = "no observations provided"
            instructions = None
            plan = None
            if observations:
                local_observations = [
                    item.model_copy(update={"timestamp": item.timestamp - start})
                    for item in observations if start <= item.timestamp <= end
                ]
                timeline = build_speaker_timeline(local_observations, end - start)
                from .media import probe_dimensions as _probe_dims
                _src_dims = _probe_dims(source)
                _src_w, _src_h = (_src_dims if _src_dims is not None else (0, 0))
                instructions = make_layout_instructions(
                    timeline, local_observations, src_w=_src_w, src_h=_src_h,
                )
                if instructions:
                    framing_mode = "speaker-follow"
                    framing_reason = f"{len(timeline)} timeline segment(s) from CLI observations"
            elif auto_framing:
                try:
                    from .autoframing import analyze_section
                    from .talknet.config import TalkNetConfig

                    try:
                        talknet_config = TalkNetConfig.resolve(redis_client)
                    except Exception:
                        talknet_config = None
                    plan = analyze_section(str(source), duration=end - start,
                                           talknet_config=talknet_config)
                    try:
                        _tn = (plan.metrics or {}).get("talknet") or {}
                        if _tn and redis_client is not None:
                            import time as _time

                            redis_client.hset("gaeclip:talknet:worker", mapping={
                                "device": str(_tn.get("device", "unknown")),
                                "status": str(_tn.get("status", "UNKNOWN")),
                                "updated_at": str(_time.time()),
                                "job_id": storage.job_root.name,
                            })
                    except Exception:
                        pass
                    logger.info(
                        "Job %s clip %s framing: mode=%s reason=%s\n%s",
                        storage.job_root.name, candidate.candidate_id,
                        plan.mode, plan.reason, plan.debug_log,
                    )
                    if plan.instructions:
                        instructions = plan.instructions
                        framing_mode = f"auto-{plan.mode}"
                        framing_reason = plan.reason
                    else:
                        framing_reason = f"auto analysis: {plan.reason}"
                except Exception as exc:
                    framing_reason = (
                        "Active Speaker Detection unavailable "
                        f"Reason: {exc} Fallback: static center crop"
                    )
                    logger.warning(
                        "Job %s clip %s framing: %s",
                        storage.job_root.name, candidate.candidate_id, framing_reason,
                    )
            task.framing_mode = framing_mode
            task.framing_reason = framing_reason
            from .framing_config import FramingConfig, resolve_encode_profile
            from .media import (render_dynamic_vertical,
                                render_trajectory_vertical, resolve_output_fps)

            render_preset, render_crf = resolve_encode_profile(
                getattr(settings, "render_profile", None))
            _fcfg = FramingConfig.from_env()
            out_fps, fps_reason = resolve_output_fps(
                source, mode=_fcfg.output_fps_mode,
                fallback=_fcfg.output_fps_fallback,
                fps_min=_fcfg.output_fps_min, fps_max=_fcfg.output_fps_max)
            logger.info(
                "Job %s clip %s render: fps=%.2f (%s) preset=%s crf=%d",
                storage.job_root.name, candidate.candidate_id,
                out_fps, fps_reason, render_preset, render_crf,
            )
            keyframes = list(getattr(plan, "keyframes", []) or [])
            routed = False
            brand_plan = None
            brand_cfg = branding or {}
            if brand_cfg.get("enabled"):
                from .branding import (
                    prepare_avatar,
                    resolve_brand_font,
                    resolve_brand_plan,
                    validate_brand_config,
                )
                from .channel_avatar import (
                    assets_dir as _brand_assets_dir,
                    fetch_channel_avatar,
                    resolve_asset_path,
                )

                try:
                    norm_brand = validate_brand_config(brand_cfg)
                    resolve_brand_font()
                    if burn_subtitles:
                        geo_w, geo_h = out_w, out_h
                    else:
                        from .media import resolve_output_geometry as _brand_geo

                        geo_w, geo_h = _brand_geo(source)
                    avatar_src = None
                    asset = norm_brand.get("avatar_asset")
                    if asset:
                        avatar_src = resolve_asset_path(
                            asset, _brand_assets_dir(settings.storage_root))
                        if avatar_src is None:
                            logger.warning(
                                "Job %s clip %s branding: asset %r tidak ditemukan; fallback tanpa avatar",
                                storage.job_root.name, candidate.candidate_id, asset)
                    if avatar_src is None and norm_brand.get("channel_name"):
                        avatar_src = fetch_channel_avatar(
                            brand_cfg.get("channel_id"),
                            brand_cfg.get("channel_url"),
                            _brand_assets_dir(settings.storage_root))
                    circle = None
                    if avatar_src is not None:
                        circle = prepare_avatar(
                            avatar_src, 512,
                            output_dir / f"{candidate.candidate_id}_avatar.png")
                    brand_plan = resolve_brand_plan(
                        norm_brand, end - start, geo_w, geo_h, circle,
                        burn_subtitles, out_fps)
                    if brand_plan is not None:
                        logger.info(
                            "Job %s clip %s branding: template=%s window=%.1f-%.1fs avatar=%s",
                            storage.job_root.name, candidate.candidate_id,
                            brand_plan.template, brand_plan.window[0],
                            brand_plan.window[1],
                            "yes" if brand_plan.show_avatar else "no")
                except ValueError as exc:
                    raise ValidationError(str(exc)) from exc
            if keyframes and len(keyframes) >= 2 and framing_mode.startswith("auto"):
                try:
                    render_trajectory_vertical(
                        source, output, keyframes, fps=out_fps,
                        duration=end - start, subtitles=subtitle_file,
                        preset=render_preset, crf=render_crf,
                        branding=brand_plan,
                    )
                    routed = True
                except Exception as exc:
                    logger.warning(
                        "Job %s clip %s trajectory render failed (%s); "
                        "falling back to legacy dynamic render",
                        storage.job_root.name, candidate.candidate_id, exc,
                    )
            if not routed:
                if instructions:
                    render_dynamic_vertical(
                        source, output, instructions, subtitles=subtitle_file,
                        fps=out_fps, preset=render_preset, crf=render_crf,
                        branding=brand_plan,
                    )
                    if debug_framing:
                        from .autoframing import dump_active_speaker_log
                        from .media import render_debug_overlay

                        boxes = [
                            (*track.box_at((end - start) / 2), track.speaker_id == instructions[0].speaker_id)
                            for track in getattr(plan, "tracks", [])
                            if track.box_at((end - start) / 2) is not None
                        ] if framing_mode.startswith("auto") else []
                        first = instructions[0]
                        render_debug_overlay(
                            source, output.with_name(f"{output.stem}_debug.mp4"),
                            boxes,
                            [(first.center_x - first.width / 2, first.center_y - first.height / 2,
                              first.width, first.height)],
                        )
                        logger.info(
                            "Job %s clip %s framing debug overlay written",
                            storage.job_root.name, candidate.candidate_id,
                        )
                else:
                    render_static_vertical(
                        source, output, start=0, duration=end - start, subtitles=subtitle_file,
                        fps=out_fps, preset=render_preset, crf=render_crf,
                        branding=brand_plan,
                    )
            try:
                size_bytes = output.stat().st_size
            except OSError:
                size_bytes = None
            _log_framing_qc(
                output, candidate.candidate_id, storage.job_root.name,
                expected_duration=end - start,
                instructions=instructions or [],
                plan=plan, debug_framing=debug_framing,
                source=source, expected_fps=out_fps,
            )
            task.status = "done"
            task.stage_detail = f"Clip {index + 1}/{total} selesai"
            task.duration_seconds = round(end - start, 3)
            task.size_bytes = size_bytes
            _sync_tasks()
            meta = _clip_metadata_best_effort(
                lines=lines, candidate=candidate, metadata=metadata,
                language=language, duration_seconds=round(end - start, 3),
                client=redis_client, settings=settings, ai_chain=ai_chain,
                job_id=storage.job_root.name, index=index, total=total,
            )
            clips.append(GeneratedClip(
                candidate_id=candidate.candidate_id, path=output,
                title=candidate.title, score=candidate.score, reason=candidate.reason,
                start=candidate.start, end=candidate.end,
                description=meta["description"],
                hashtags=tuple(meta["hashtags"]),
                metadata_source=meta["source"],
                metadata_updated_at=meta["updated_at"],
            ))
        except CancelledError:
            raise
        except Exception as exc:
            from .errors import GaeclipError as _GaeclipError
            code = getattr(exc, "code", "JOB_FAILED") if isinstance(exc, _GaeclipError) else "JOB_FAILED"
            task.status = "failed"
            task.stage_detail = f"Clip {index + 1}/{total} gagal"
            task.error_code = code
            task.error_message = str(exc)
            if task not in clip_tasks:
                clip_tasks.append(task)
            _sync_tasks()
            _emit("rendering", 75 + int(20 * index / max(1, total)),
                  f"Clip {index + 1}/{total} gagal: {exc}", index + 1, total)
            continue
    if not clips and clip_tasks:
        first_error = next((task for task in clip_tasks if task.status == "failed"), None)
        if first_error is not None and first_error.error_message:
            raise ValidationError(f"Semua clip gagal. Kesalahan pertama: {first_error.error_message}")
    return GenerationResult(
        job_id=storage.job_root.name, source=source_dir, curation=curation,
        clips=clips, clip_tasks=clip_tasks,
        transcript_reused=transcript_reused,
        transcript_cache_id=transcript_cache_id,
        transcript_engine=transcript_engine,
    )
