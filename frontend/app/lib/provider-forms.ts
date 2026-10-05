/**
 * Pure provider-form helpers (no React): edit payload building, name
 * validation, and duplicate-model guards. Backend contract:
 * - PATCH /providers/{id} identifies by STABLE id (`gemini_api_N`).
 * - `name` is display-only and freely renameable.
 * - `api_key` is replaced ONLY when a non-blank value is sent; omitting it
 *   (or sending "") preserves the stored secret.
 */

export interface ProviderEditInput {
  name: string;
  /** Raw API-key field from the edit dialog; "" / whitespace = unchanged. */
  apiKey: string;
  enabled: boolean;
  priority: number;
}

export interface ProviderEditPayload {
  name: string;
  enabled: boolean;
  priority: number;
  /** Present ONLY when the user actually typed a new key. */
  api_key?: string;
  [key: string]: string | number | boolean | undefined;
}

/** Display-name validation for the edit dialog. Null = valid. */
export function validateProviderName(name: string): string | null {
  if (name.trim() === "") return "Nama provider tidak boleh kosong";
  if (name.trim().length > 80) return "Nama provider maksimal 80 karakter";
  return null;
}

/**
 * Build the PATCH body. The secret is NEVER echoed back: when the key field
 * is blank we omit `api_key` entirely so the backend keeps the old secret.
 */
export function buildProviderEditPayload(input: ProviderEditInput): ProviderEditPayload {
  const payload: ProviderEditPayload = {
    name: input.name.trim(),
    enabled: input.enabled,
    priority: Math.max(1, Math.floor(input.priority) || 1),
  };
  if (input.apiKey.trim() !== "") {
    payload.api_key = input.apiKey.trim();
  }
  return payload;
}

/** True when `candidate` already exists in the provider's model list. */
export function isDuplicateModel(existingIds: readonly string[], candidate: string): boolean {
  const needle = candidate.trim();
  if (needle === "") return false;
  return existingIds.some((id) => id === needle);
}

export function normalizeModelId(candidate: string): string {
  return candidate.trim();
}
