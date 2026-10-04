"""Pure framing geometry + quality guard (Phase 1).

Dependency-free and ffmpeg-free so every function is unit-testable.
All coordinates are normalized source fractions (0-1) unless suffixed
``_px``. The renderer (``media.py``) keeps its own pixel computation;
these helpers drive *analysis-side* decisions (group/pane windows,
guard verdicts) with the same aspect-first rule: CROP first to the
output aspect, then UNIFORM SCALE. Never the reverse.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CropWindow:
    """Normalized source-space crop rectangle."""

    x: float  # left
    y: float  # top
    width: float
    height: float

    @property
    def x2(self) -> float:
        return self.x + self.width

    @property
    def y2(self) -> float:
        return self.y + self.height

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2, self.y + self.height / 2)

    def is_valid(self) -> bool:
        return (
            self.width > 0 and self.height > 0
            and self.x >= 0 and self.y >= 0
            and self.x2 <= 1.0 + 1e-9 and self.y2 <= 1.0 + 1e-9
        )


def aspect_matches(width: float, height: float, out_w: float, out_h: float,
                   tol: float = 0.01) -> bool:
    """Aspect equality within tolerance (covers pixel even-rounding)."""
    if width <= 0 or height <= 0 or out_w <= 0 or out_h <= 0:
        return False
    return abs((width / height) - (out_w / out_h)) <= tol


def portrait_window_size(src_w: int, src_h: int, out_w: int = 1080,
                         out_h: int = 1920) -> tuple[int, int]:
    """Largest ``out_w:out_h`` pixel window fitting ``src_w x src_h``.

    Same rule as the renderer's ``compute_vertical_ladder`` (aspect-first,
    ceiling to even bounded by source) so analysis pixel truth always
    matches what the renderer will cut.
    """
    import math

    target = out_w / out_h
    crop_w = min(float(src_w), src_h * target)
    crop_h = crop_w / target
    if crop_h > src_h:
        crop_h = float(src_h)
        crop_w = crop_h * target
    crop_w_ceil = math.ceil(crop_w / 2) * 2
    crop_w_floor = int(crop_w) // 2 * 2
    crop_w = crop_w_ceil if crop_w_ceil <= src_w else crop_w_floor
    crop_h_ceil = math.ceil(crop_h / 2) * 2
    crop_h_floor = int(crop_h) // 2 * 2
    crop_h = crop_h_ceil if crop_h_ceil <= src_h else crop_h_floor
    return (max(2, min(crop_w, src_w // 2 * 2)),
            max(2, min(crop_h, src_h // 2 * 2)))


def portrait_crop_for_center(center_x: float, center_y: float,
                             src_w: int, src_h: int,
                             out_w: int = 1080,
                             out_h: int = 1920) -> CropWindow:
    """Aspect-correct window positioned on ``(center_x, center_y)``."""
    crop_w_px, crop_h_px = portrait_window_size(src_w, src_h, out_w, out_h)
    half_w = (crop_w_px / src_w) / 2
    half_h = (crop_h_px / src_h) / 2
    half_w = min(half_w, 0.5)
    half_h = min(half_h, 0.5)
    cx = min(1.0 - half_w, max(half_w, center_x))
    cy = min(1.0 - half_h, max(half_h, center_y))
    return CropWindow(x=cx - half_w, y=cy - half_h,
                      width=half_w * 2, height=half_h * 2)


def _union_box(faces: list[tuple[float, float, float, float]]
               ) -> tuple[float, float, float, float]:
    return (
        min(box[0] for box in faces),
        min(box[1] for box in faces),
        max(box[2] for box in faces),
        max(box[3] for box in faces),
    )


def face_visibility(face: tuple[float, float, float, float],
                    crop: CropWindow) -> float:
    """Fraction of face area inside the crop (1.0 = fully visible)."""
    ix1, iy1 = max(face[0], crop.x), max(face[1], crop.y)
    ix2, iy2 = min(face[2], crop.x2), min(face[3], crop.y2)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    area = max(1e-9, (face[2] - face[0]) * (face[3] - face[1]))
    return inter / area


def _fit_window(x1: float, y1: float, x2: float, y2: float,
                src_w: int, src_h: int, out_w: int, out_h: int) -> CropWindow:
    """Smallest aspect-correct window covering rect, uniformly shrunk to fit.

    All sizing happens in SOURCE PIXELS (normalized units are not square
    unless the source is square): grow to cover at the exact output aspect,
    then scale down uniformly if larger than the source. Aspect is therefore
    exact by construction; callers convert back to normalized units.
    """
    rx1, ry1, rx2, ry2 = x1 * src_w, y1 * src_h, x2 * src_w, y2 * src_h
    req_w = max(1.0, rx2 - rx1)
    req_h = max(1.0, ry2 - ry1)
    target = out_w / out_h
    if req_w / req_h > target:
        width, height = req_w, req_w / target
    else:
        width, height = req_h * target, req_h
    overshoot = max(width / src_w, height / src_h, 1e-9)
    if overshoot > 1.0:
        width, height = width / overshoot, height / overshoot
    cx = min(src_w - width / 2, max(width / 2, (rx1 + rx2) / 2))
    cy = min(src_h - height / 2, max(height / 2, (ry1 + ry2) / 2))
    return CropWindow(x=(cx - width / 2) / src_w, y=(cy - height / 2) / src_h,
                      width=width / src_w, height=height / src_h)


def safe_group_crop(faces: list[tuple[float, float, float, float]],
                    margin: float = 0.06,
                    out_w: int = 1080, out_h: int = 1920,
                    visibility_min: float = 0.85,
                    src_w: int = 1920, src_h: int = 1080,
                    ) -> tuple[CropWindow, dict]:
    """Group window that never amputates critical faces.

    union -> margin -> aspect fit -> clamp -> validate -> widen (reduce
    zoom) -> safest fallback. Returns (window, report) where report holds
    per-face visibility and the reason string. Sizing is source-aware
    (pixel aspect), so the result always matches the output aspect.
    """
    if not faces:
        raise ValueError("safe_group_crop requires at least one face")
    x1, y1, x2, y2 = _union_box(faces)
    x1 = max(0.0, x1 - margin)
    y1 = max(0.0, y1 - margin)
    x2 = min(1.0, x2 + margin)
    y2 = min(1.0, y2 + margin)

    window = _fit_window(x1, y1, x2, y2, src_w, src_h, out_w, out_h)
    visibility = [face_visibility(face, window) for face in faces]
    if all(value >= visibility_min for value in visibility):
        return window, {"visibility": visibility, "reason": "union-fit"}

    # Clamp may have cut a face: widen toward the full frame (reduce zoom).
    widened = _fit_window(
        max(0.0, x1 - margin), max(0.0, y1 - margin),
        min(1.0, x2 + margin), min(1.0, y2 + margin),
        src_w, src_h, out_w, out_h,
    )
    visibility = [face_visibility(face, widened) for face in faces]
    if all(value >= visibility_min for value in visibility):
        return widened, {"visibility": visibility, "reason": "widened"}

    # Safest possible: maximal aspect-correct window (renderer-agreed size)
    # centered on the UNION center (best-effort balance). Never
    # frame-center: that can silently favor one side when faces spread out.
    max_w_px, max_h_px = portrait_window_size(src_w, src_h, out_w, out_h)
    sw, sh = max_w_px / src_w, max_h_px / src_h
    ucx = min(1.0 - sw / 2, max(sw / 2, (x1 + x2) / 2))
    ucy = min(1.0 - sh / 2, max(sh / 2, (y1 + y2) / 2))
    safest = CropWindow(x=ucx - sw / 2, y=ucy - sh / 2, width=sw, height=sh)
    visibility = [face_visibility(face, safest) for face in faces]
    return safest, {"visibility": visibility, "reason": "safest-max-window"}


def split_pane_crop(center_x: float, src_w: int, src_h: int,
                    out_w: int = 1080, pane_h: int = 960) -> CropWindow:
    """Aspect-preserving source window for one split-screen pane.

    The pane displays ``out_w x pane_h``; the source window keeps exactly
    that aspect (crop-first, uniform scale later) instead of squashing the
    full frame. Horizontally centered on the speaker.
    """
    crop_w_from_h = src_h * out_w / pane_h
    crop_h_from_w = src_w * pane_h / out_w
    if crop_w_from_h <= src_w:
        width, height = crop_w_from_h / src_w, 1.0
    else:
        width, height = 1.0, crop_h_from_w / src_h
    width, height = min(1.0, width), min(1.0, height)
    cx = min(1.0 - width / 2, max(width / 2, center_x))
    return CropWindow(x=cx - width / 2, y=(1.0 - height) / 2,
                      width=width, height=height)


def upscale_factor(crop_width_px: float, out_w: int = 1080) -> float:
    """Uniform upscale implied by rendering this crop at ``out_w``."""
    if crop_width_px <= 0:
        return float("inf")
    return out_w / crop_width_px


def validate_crop(window: CropWindow,
                  src_w: int, src_h: int,
                  out_w: int = 1080, out_h: int = 1920,
                  faces: list[tuple[float, float, float, float]] | None = None,
                  max_upscale: float = 2.0,
                  visibility_min: float = 0.85) -> tuple[bool, str, str]:
    """Quality guard. Returns (ok, reason, suggestion).

    suggestion in {"ok", "widen", "fallback"}: "widen" means retry with a
    larger window; "fallback" means use the safest static crop.
    """
    if window.width <= 0 or window.height <= 0:
        return False, "zero-sized crop", "fallback"
    if window.x < 0 or window.y < 0 or window.x2 > 1.0 + 1e-9 or window.y2 > 1.0 + 1e-9:
        return False, "crop outside source bounds", "fallback"
    if not aspect_matches(window.width * src_w, window.height * src_h,
                          out_w, out_h):
        return False, "crop aspect does not match output aspect", "fallback"
    factor = upscale_factor(window.width * src_w, out_w)
    if factor > max_upscale:
        return False, f"upscale {factor:.2f}x exceeds {max_upscale:g}x", "widen"
    for face in faces or []:
        if face_visibility(face, window) < visibility_min:
            return False, "target face mostly outside crop", "widen"
    return True, "ok", "ok"


def compute_target_anchor(box: tuple[float, float, float, float],
                          headroom_factor: float = 0.18,
                          head_anchor_ratio: float = 0.35,
                          crop_height: float = 1.0) -> tuple[float, float]:
    """Stable single-target anchor from a face box (normalized coords).

    Horizontal: face center (looking-room offsets stay downstream).
    Vertical: face_top + HEAD_ANCHOR_RATIO * height, then the same
    headroom shift the pipeline has always applied, clamped so the
    anchor never demands an out-of-range crop center. Using the face
    TOP (not the jitter-prone bbox center) keeps foreheads framed when
    detectors wobble box height.
    """
    x1, y1, x2, y2 = box
    face_height = max(1e-6, y2 - y1)
    cx = (x1 + x2) / 2
    cy = max(crop_height / 2, y1 + face_height * head_anchor_ratio
             - face_height * headroom_factor)
    return cx, cy


# ---------------------------------------------------------------------------
# Phase 4.1: two-speaker co-framing with strict composition safety.
# Pure geometry (no I/O, no detector); evidence/temporal gates live in
# autoframing. A correct SINGLE always beats a bad pair shot: hard
# constraints reject first, soft score only ranks surviving candidates.
# Midpoint is ONE candidate among seven, never the final answer.
# ---------------------------------------------------------------------------

@dataclass
class TwoSpeakerResult:
    """Outcome of a two-speaker composition search (single 9:16 crop)."""

    feasible: bool
    mode: str  # "two_speaker" when feasible else "single"
    center_x: float
    center_y: float
    width: float
    height: float
    confidence: float  # composition score 0-1 (0 when rejected)
    reason: str  # accept cause or specific reject reason (§38)
    metrics: dict


def _edge_margin(face: tuple[float, float, float, float],
                 crop: CropWindow) -> float:
    """Minimum face-to-crop-edge clearance (normalized units). Negative
    means the face sticks out past the crop."""
    return min(face[0] - crop.x, crop.x2 - face[2],
               face[1] - crop.y, crop.y2 - face[3])


def _empty_space_ratio(faces: list[tuple[float, float, float, float]],
                       crop: CropWindow) -> float:
    """Horizontal gap waste between faces relative to crop width (0-1).

    Only the gap BETWEEN faces counts: vertical headroom/body room is
    inherent to 9:16 and must not penalize close pairs. A wide empty
    center (camera parked between two far speakers) scores near 1.
    """
    if len(faces) < 2:
        return 0.0
    ordered = sorted(faces, key=lambda b: (b[0] + b[2]) / 2)
    gap = max(0.0, ordered[1][0] - ordered[0][2])
    return max(0.0, min(1.0, gap / max(crop.width, 1e-9)))


def _two_speaker_candidates(box_a: tuple[float, float, float, float],
                            box_b: tuple[float, float, float, float],
                            src_w: int, src_h: int,
                            out_w: int, out_h: int,
                            margin: float = 0.03,
                            ) -> list[tuple[str, CropWindow]]:
    """Seven aspect-correct candidates (§12). Midpoint is first, never final:
    every candidate goes through the same hard validator."""
    cax = (box_a[0] + box_a[2]) / 2
    cay = (box_a[1] + box_a[3]) / 2
    cbx = (box_b[0] + box_b[2]) / 2
    cby = (box_b[1] + box_b[3]) / 2
    mid_x, mid_y = (cax + cbx) / 2, (cay + cby) / 2
    ux1, uy1 = min(box_a[0], box_b[0]), min(box_a[1], box_b[1])
    ux2, uy2 = max(box_a[2], box_b[2]), max(box_a[3], box_b[3])
    gap = abs(cbx - cax)
    out = [
        ("midpoint", portrait_crop_for_center(mid_x, mid_y, src_w, src_h,
                                              out_w, out_h)),
        ("union", _fit_window(max(0.0, ux1 - margin), max(0.0, uy1 - margin),
                              min(1.0, ux2 + margin), min(1.0, uy2 + margin),
                              src_w, src_h, out_w, out_h)),
        ("left_biased", portrait_crop_for_center(
            mid_x - 0.25 * gap, mid_y, src_w, src_h, out_w, out_h)),
        ("right_biased", portrait_crop_for_center(
            mid_x + 0.25 * gap, mid_y, src_w, src_h, out_w, out_h)),
        ("a_priority", portrait_crop_for_center(cax, cay, src_w, src_h,
                                                out_w, out_h)),
        ("b_priority", portrait_crop_for_center(cbx, cby, src_w, src_h,
                                                out_w, out_h)),
        ("composition", _fit_window(
            max(0.0, ux1 - margin), max(0.0, uy1 - margin * 2),
            min(1.0, ux2 + margin), min(1.0, uy2 + margin),
            src_w, src_h, out_w, out_h)),
    ]
    return out


def compute_two_speaker_composition(
    box_a: tuple[float, float, float, float],
    box_b: tuple[float, float, float, float],
    src_w: int, src_h: int,
    out_w: int = 1080, out_h: int = 1920,
    max_distance: float = 0.45,
    min_face_width: float = 0.06,
    min_face_height: float = 0.10,
    min_edge_margin: float = 0.02,
    max_upscale: float = 2.0,
    max_crop_width: float = 0.60,
    max_empty_space: float = 0.45,
    max_cutoff: float = 0.15,
    max_gap_ratio: float = 0.50,
    visibility_threshold: float = 0.85,
    min_score: float = 0.55,
    movement_penalty: float = 0.10,
    empty_space_penalty: float = 0.50,
    face_size_penalty: float = 0.40,
    prev_center: tuple[float, float] | None = None,
    margin: float = 0.03,
) -> TwoSpeakerResult:
    """Find a valid single 9:16 crop holding exactly A+B, or reject.

    Hard constraints (any failure → feasible=False with a specific
    reason): too_far, face_too_small, speaker_invisible, edge_cutoff,
    excessive_upscale, excessive_crop_width, empty_space, no_valid_crop,
    person_free_crop. Soft score only ranks survivors; a high score never
    revives a rejected candidate (§25).
    """
    faces = [box_a, box_b]
    widths = [b[2] - b[0] for b in faces]
    heights = [b[3] - b[1] for b in faces]
    centers = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b in faces]
    distance = abs(centers[1][0] - centers[0][0])
    if distance > max_distance:
        return TwoSpeakerResult(False, "single", 0.5, 0.5, 0.0, 0.0, 0.0,
                                "too_far",
                                {"distance": round(distance, 4),
                                 "max_distance": max_distance})
    for index, (width, height) in enumerate(zip(widths, heights)):
        if width < min_face_width or height < min_face_height:
            return TwoSpeakerResult(False, "single", 0.5, 0.5, 0.0, 0.0,
                                    0.0, "face_too_small",
                                    {"face": index,
                                     "face_size": (round(width, 4),
                                                   round(height, 4))})
    crop_w_px, _ = portrait_window_size(src_w, src_h, out_w, out_h)
    required_upscale = upscale_factor(crop_w_px, out_w)
    evaluated: list[tuple[float, str, CropWindow, dict]] = []
    first_reject = "no_valid_crop"
    saw_candidate = False
    for name, crop in _two_speaker_candidates(
            box_a, box_b, src_w, src_h, out_w, out_h, margin):
        saw_candidate = True
        vis_a = face_visibility(box_a, crop)
        vis_b = face_visibility(box_b, crop)
        cutoff = max(1.0 - vis_a, 1.0 - vis_b)
        if vis_a < visibility_threshold or vis_b < visibility_threshold:
            if first_reject == "no_valid_crop":
                first_reject = ("edge_cutoff" if cutoff > max_cutoff
                                else "speaker_invisible")
            continue
        if cutoff > max_cutoff:
            if first_reject == "no_valid_crop":
                first_reject = "edge_cutoff"
            continue
        edge = min(_edge_margin(box_a, crop), _edge_margin(box_b, crop))
        if edge < min_edge_margin:
            if first_reject == "no_valid_crop":
                first_reject = "edge_cutoff"
            continue
        if upscale_factor(crop.width * src_w, out_w) > max_upscale:
            if first_reject == "no_valid_crop":
                first_reject = "excessive_upscale"
            continue
        if crop.width > max_crop_width and crop.width < 0.99:
            # Portrait sources fill the full width by construction; the
            # width cap only constrains landscape crops.
            if first_reject == "no_valid_crop":
                first_reject = "excessive_crop_width"
            continue
        empty = _empty_space_ratio(faces, crop)
        if empty > max_empty_space:
            if first_reject == "no_valid_crop":
                first_reject = "empty_space"
            continue
        # Phase 4.2 empty-center protection (§53): the void BETWEEN the
        # faces over crop width. Two tiny people around a void is not a
        # conversation shot — reject even if margins technically hold.
        ordered = sorted(faces, key=lambda b: (b[0] + b[2]) / 2)
        gap_ratio = max(0.0, ordered[1][0] - ordered[0][2]) / max(crop.width, 1e-9)
        if gap_ratio > max_gap_ratio:
            if first_reject == "no_valid_crop":
                first_reject = "empty_center"
            continue
        if not any(v >= 0.5 for v in (vis_a, vis_b)):
            if first_reject == "no_valid_crop":
                first_reject = "person_free_crop"
            continue
        # Soft score (§24): visibility + size + headroom + edge + composition
        # minus empty/upscale/movement/width/small-face penalties.
        head_a = box_a[1] - crop.y
        head_b = box_b[1] - crop.y
        headroom = min(1.0, max(0.0, (head_a + head_b) / 2 / max(crop.height, 1e-9)
                                * 4.0))
        face_size = min(1.0, (min(widths) / max(min_face_width, 1e-9)
                              + min(heights) / max(min_face_height, 1e-9)) / 4.0
                        + 0.5)
        edge_safety = min(1.0, max(0.0, edge / 0.10))
        movement = 0.0
        if prev_center is not None:
            movement = ((crop.center[0] - prev_center[0]) ** 2
                        + (crop.center[1] - prev_center[1]) ** 2) ** 0.5
        score = ((vis_a + vis_b) / 2 * 0.35
                 + min(1.0, face_size) * 0.20
                 + headroom * 0.10
                 + edge_safety * 0.10
                 + 0.25
                 - empty * empty_space_penalty * 0.5
                 - movement * movement_penalty * 2.0
                 - max(0.0, crop.width - 0.35) * 0.5
                 - max(0.0, 0.12 - min(widths)) * face_size_penalty * 2.0)
        score = max(0.0, min(1.0, score))
        evaluated.append((score, name, crop, {
            "visibility_a": round(vis_a, 3), "visibility_b": round(vis_b, 3),
            "edge_margin": round(edge, 4), "empty_space": round(empty, 3),
            "cutoff": round(cutoff, 3),
            "upscale": round(upscale_factor(crop.width * src_w, out_w), 3),
            "crop_width": round(crop.width, 4),
            "movement_cost": round(movement, 4),
        }))
    if not evaluated:
        return TwoSpeakerResult(False, "single", 0.5, 0.5, 0.0, 0.0, 0.0,
                                first_reject if saw_candidate else "no_valid_crop",
                                {"distance": round(distance, 4)})
    evaluated.sort(key=lambda item: -item[0])
    best_score, best_name, best_crop, best_met = evaluated[0]
    _ordered_faces = sorted(faces, key=lambda b: (b[0] + b[2]) / 2)
    _gap = max(0.0, _ordered_faces[1][0] - _ordered_faces[0][2])
    metrics = {
        "face_size_a": (round(widths[0], 4), round(heights[0], 4)),
        "face_size_b": (round(widths[1], 4), round(heights[1], 4)),
        "edge_margin_a": round(_edge_margin(box_a, best_crop), 4),
        "edge_margin_b": round(_edge_margin(box_b, best_crop), 4),
        "headroom_a": round(box_a[1] - best_crop.y, 4),
        "headroom_b": round(box_b[1] - best_crop.y, 4),
        "speaker_visibility_a": best_met["visibility_a"],
        "speaker_visibility_b": best_met["visibility_b"],
        "empty_space_ratio": best_met["empty_space"],
        "required_crop_width": best_met["crop_width"],
        "required_upscale": best_met["upscale"],
        "composition_score": round(best_score, 3),
        "movement_cost": best_met["movement_cost"],
        "cutoff_ratio": best_met["cutoff"],
        "gap_ratio": round(_gap / max(best_crop.width, 1e-9), 3),
        "candidate": best_name,
        "candidates_evaluated": len(evaluated),
    }
    if best_score < min_score:
        return TwoSpeakerResult(False, "single", 0.5, 0.5, 0.0, 0.0,
                                round(best_score, 3), "score_below_threshold",
                                metrics)
    cx, cy = best_crop.center
    return TwoSpeakerResult(True, "two_speaker", cx, cy,
                            best_crop.width, best_crop.height,
                            round(best_score, 3), f"accept:{best_name}",
                            metrics)
