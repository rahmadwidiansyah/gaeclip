/**
 * Pure pagination helpers shared by every settings list.
 *
 * Contract (tested):
 * - page is 1-based; pageSize >= 1
 * - pageCount(total, pageSize) = ceil(total / pageSize), min 1 when total = 0
 *   (callers hide the control when total <= pageSize anyway)
 * - clampPage keeps a page inside [1, pageCount]
 * - paginate slices AFTER filtering: all -> filtered -> page slice
 */

export const DEFAULT_PAGE_SIZE = 20;

export const PAGE_SIZE_OPTIONS = [10, 20, 50] as const;

export function pageCount(total: number, pageSize: number): number {
  const safeSize = Math.max(1, Math.floor(pageSize) || 1);
  const safeTotal = Math.max(0, Math.floor(total) || 0);
  return Math.max(1, Math.ceil(safeTotal / safeSize));
}

export function clampPage(page: number, totalPages: number): number {
  const safeTotal = Math.max(1, Math.floor(totalPages) || 1);
  const p = Math.floor(page) || 1;
  return Math.min(safeTotal, Math.max(1, p));
}

export function paginate<T>(items: readonly T[], page: number, pageSize: number): T[] {
  const safeSize = Math.max(1, Math.floor(pageSize) || 1);
  const total = pageCount(items.length, safeSize);
  const safePage = clampPage(page, total);
  const start = (safePage - 1) * safeSize;
  return items.slice(start, start + safeSize);
}

/** First item number (1-based) shown on a page; 0 when the list is empty. */
export function pageRangeStart(total: number, page: number, pageSize: number): number {
  if (total <= 0) return 0;
  return (clampPage(page, pageCount(total, pageSize)) - 1) * Math.max(1, pageSize) + 1;
}

/** Last item number (1-based) shown on a page; 0 when the list is empty. */
export function pageRangeEnd(total: number, page: number, pageSize: number): number {
  if (total <= 0) return 0;
  return Math.min(total, pageRangeStart(total, page, pageSize) + Math.max(1, pageSize) - 1);
}
