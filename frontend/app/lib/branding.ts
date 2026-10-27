/**
 * Clip branding preview model (pure, unit-tested, no network).
 *
 * Single source for template/position/timing options shared by the
 * Create Clip form and the 9:16 preview. Mirrors the backend contract in
 * `src/gaeclip/branding.py` (TEMPLATES / POSITIONS / TIMINGS / ANIMATIONS);
 * the preview is a CSS simulation — blur amounts and FFmpeg fade curves
 * are approximated, never rendered.
 */

export type BrandTemplate = "glass_end" | "minimal_corner" | "creator_tag" | "creator_outro";
export type BrandDisplayMode = "avatar_name" | "avatar_only" | "name_only";
export type BrandPosition =
  | "top_left"
  | "top_right"
  | "bottom_left"
  | "bottom_right"
  | "top_center"
  | "center";
export type BrandTiming = "full" | "first" | "last";
export type BrandAnimation = "none" | "fade" | "fade_scale";

export const BRAND_TEMPLATES: { id: BrandTemplate; label: string; hint: string }[] = [
  { id: "glass_end", label: "Glass End Card", hint: "Elegan di 1–2 detik terakhir" },
  { id: "minimal_corner", label: "Minimal Corner", hint: "Kecil di sudut video" },
  { id: "creator_tag", label: "Creator Tag", hint: "Pill kaca di area atas" },
  { id: "creator_outro", label: "Creator Outro", hint: "Fullscreen di akhir video" },
];

export const BRAND_POSITIONS: { id: BrandPosition; label: string }[] = [
  { id: "top_left", label: "Kiri atas" },
  { id: "top_right", label: "Kanan atas" },
  { id: "bottom_left", label: "Kiri bawah" },
  { id: "bottom_right", label: "Kanan bawah" },
  { id: "top_center", label: "Tengah atas" },
  { id: "center", label: "Tengah" },
];

export const BRAND_TIMINGS: { id: BrandTiming; label: string }[] = [
  { id: "full", label: "Sepanjang video" },
  { id: "first", label: "Awal video" },
  { id: "last", label: "Akhir video" },
];

export const BRAND_ANIMATIONS: { id: BrandAnimation; label: string }[] = [
  { id: "none", label: "Tanpa animasi" },
  { id: "fade", label: "Fade" },
  { id: "fade_scale", label: "Fade + scale" },
];

export const BRAND_DISPLAY_MODES: { id: BrandDisplayMode; label: string }[] = [
  { id: "avatar_name", label: "Avatar + nama" },
  { id: "avatar_only", label: "Avatar saja" },
  { id: "name_only", label: "Nama saja" },
];

export const BRAND_DURATIONS = [1, 1.5, 2, 3] as const;

export interface BrandingForm {
  enabled: boolean;
  template: BrandTemplate;
  channelName: string;
  avatarAsset: string;
  avatarPreviewUrl: string;
  displayMode: BrandDisplayMode;
  position: BrandPosition;
  timing: BrandTiming;
  duration: number;
  blur: number;
  dim: number;
  animation: BrandAnimation;
  additionalText: string;
}

export const DEFAULT_BRANDING: BrandingForm = {
  enabled: false,
  template: "glass_end",
  channelName: "",
  avatarAsset: "",
  avatarPreviewUrl: "",
  displayMode: "avatar_name",
  position: "top_left",
  timing: "last",
  duration: 2,
  blur: 20,
  dim: 0.35,
  animation: "fade",
  additionalText: "",
};

const TEMPLATE_SET = new Set<string>(BRAND_TEMPLATES.map((t) => t.id));
const POSITION_SET = new Set<string>(BRAND_POSITIONS.map((p) => p.id));

export interface BrandingPreviewSpec {
  /** CSS justify-content for vertical placement inside the 9:16 frame. */
  justify: "flex-start" | "center" | "flex-end";
  /** Avatar diameter as % of frame width (0 when hidden). */
  avatarPct: number;
  /** True when the template paints a blurred backdrop. */
  hasBlurBg: boolean;
  /** Fallback initial when no avatar image is available. */
  fallbackInitial: string;
  /** aria-label for the preview region. */
  ariaLabel: string;
  /** Whether name text is shown. */
  showName: boolean;
  /** Whether avatar is shown. */
  showAvatar: boolean;
}

export function isEndcardTemplate(template: string): boolean {
  return template === "glass_end" || template === "creator_outro";
}

export function isCornerTemplate(template: string): boolean {
  return template === "minimal_corner" || template === "creator_tag";
}

export function clampBrandDuration(value: number): number {
  if (!Number.isFinite(value)) return 2;
  return Math.min(10, Math.max(0.5, value));
}

export function resolveBrandingPreview(form: BrandingForm): BrandingPreviewSpec {
  if (!TEMPLATE_SET.has(form.template)) {
    throw new Error(`unknown brand template: ${form.template}`);
  }
  if (!POSITION_SET.has(form.position)) {
    throw new Error(`unknown brand position: ${form.position}`);
  }
  const endcard = isEndcardTemplate(form.template);
  const showAvatar = form.displayMode !== "name_only";
  const showName =
    form.displayMode !== "avatar_only" && form.channelName.trim().length > 0;
  const name = form.channelName.trim();
  let justify: BrandingPreviewSpec["justify"] = "flex-start";
  if (endcard || form.position === "center") {
    justify = "center";
  } else if (
    form.position === "bottom_left" ||
    form.position === "bottom_right"
  ) {
    justify = "flex-end";
  }
  const avatarPct =
    !showAvatar ? 0 : form.template === "creator_outro" ? 30 : form.template === "glass_end" ? 22 : 12;
  return {
    justify,
    avatarPct,
    hasBlurBg: endcard,
    fallbackInitial: (name.charAt(0) || "?").toUpperCase(),
    ariaLabel: `Pratinjau branding ${form.template}`,
    showName,
    showAvatar,
  };
}

/** Cheap client-side plausibility check (backend validates for real). */
export function isProbablyYoutubeUrl(value: string): boolean {
  const v = (value || "").trim();
  if (!/^https?:\/\//i.test(v)) return false;
  try {
    const u = new URL(v);
    const host = u.hostname.toLowerCase();
    if (host === "youtu.be") return u.pathname.replace(/\//g, "").length > 0;
    if (host === "www.youtube.com" || host === "youtube.com" || host === "m.youtube.com") {
      return u.pathname === "/watch" || u.pathname.startsWith("/shorts/") || u.pathname.startsWith("/live/");
    }
    return false;
  } catch {
    return false;
  }
}

/** Human-readable window label, e.g. "2 dtk terakhir". */
export function brandWindowLabel(form: BrandingForm): string {
  const d = `${form.duration} dtk`;
  if (isEndcardTemplate(form.template)) return `${d} terakhir`;
  if (form.timing === "full") return "Sepanjang video";
  if (form.timing === "first") return `${d} pertama`;
  return `${d} terakhir`;
}
