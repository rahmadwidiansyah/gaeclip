"use client";

/**
 * Single icon system: every UI icon resolves to a Lucide component.
 * Call sites use stable short names via <Icon name="..." /> (components/ui).
 * Native HTML5 video controls are intentionally untouched.
 */

import {
  ArrowUpDown,
  BriefcaseBusiness,
  Calendar,
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
  Image,
  Info,
  LayoutDashboard,
  LayoutGrid,
  Link,
  ListFilter,
  LoaderCircle,
  Monitor,
  Moon,
  Play,
  Plus,
  RefreshCw,
  Search,
  Settings,
  Star,
  Sun,
  Trash2,
  TriangleAlert,
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
  circle: Circle,
  loader: LoaderCircle,
} satisfies Record<string, LucideIcon>;

export type IconName = keyof typeof ICONS | (string & {});

export function resolveIcon(name: string): LucideIcon {
  return (ICONS as Record<string, LucideIcon>)[name] ?? BriefcaseBusiness;
}
