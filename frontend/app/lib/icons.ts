"use client";

/**
 * Single icon system: every UI icon resolves to a Lucide component.
 * Call sites use stable short names via <Icon name="..." /> (components/ui).
 * Native HTML5 video controls are intentionally untouched.
 */

import {
  ArrowUpDown,
  BadgeCheck,
  BriefcaseBusiness,
  Calendar,
  Camera,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  Circle,
  Clapperboard,
  Clock,
  Copy,
  Download,
  Eye,
  Image,
  Info,
  LayoutDashboard,
  LayoutGrid,
  Link,
  ListFilter,
  LoaderCircle,
  Monitor,
  Moon,
  Music2,
  Palette,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  Search,
  Settings,
  Star,
  Sun,
  ThumbsUp,
  Trash2,
  TriangleAlert,
  Type,
  Upload,
  X,
  type LucideIcon,
} from "lucide-react";

export const ICONS = {
  dashboard: LayoutDashboard,
  jobs: BriefcaseBusiness,
  clips: Clapperboard,
  settings: Settings,
  plus: Plus,
  search: Search,
  download: Download,
  close: X,
  check: Check,
  alert: TriangleAlert,
  clock: Clock,
  chevL: ChevronLeft,
  chevR: ChevronRight,
  chevD: ChevronDown,
  chevU: ChevronUp,
  edit: Pencil,
  refresh: RefreshCw,
  trash: Trash2,
  sun: Sun,
  moon: Moon,
  monitor: Monitor,
  play: Play,
  link: Link,
  filter: ListFilter,
  sort: ArrowUpDown,
  group: LayoutGrid,
  calendar: Calendar,
  image: Image,
  copy: Copy,
  star: Star,
  info: Info,
  upload: Upload,
  badge: BadgeCheck,
  palette: Palette,
  type: Type,
  eye: Eye,
  /* Platform glyphs: generic Lucide metaphors (brand icons were removed
     upstream), always paired with the platform name text — never a fake logo. */
  youtube: Play,
  tiktok: Music2,
  instagram: Camera,
  facebook: ThumbsUp,
  circle: Circle,
  loader: LoaderCircle,
} satisfies Record<string, LucideIcon>;

export type IconName = keyof typeof ICONS | (string & {});

export function resolveIcon(name: string): LucideIcon {
  return (ICONS as Record<string, LucideIcon>)[name] ?? BriefcaseBusiness;
}
