import { z } from 'zod';

/**
 * Centralna sekcja Ustawienia panelu admina.
 * Kontrakt: docs/spec-landing/ustawienia-katalog.md sekcja 7
 * (GET/PUT /api/admin/settings, GET/POST /api/admin/connections).
 * Sekrety nigdy nie wychodzą - status-only przez connections.
 */

// ── TUNABLE (skalar nadpisujący env) ───────────────────────────────────────────

/** Źródło efektywnej wartości: panel (db) > env > bezpieczny default. */
export const settingSourceSchema = z.enum(['db', 'env', 'default']);
export type SettingSource = z.infer<typeof settingSourceSchema>;

export const tunableStateSchema = z.object({
  value: z.union([z.string(), z.number(), z.null()]),
  source: settingSourceSchema,
  envFallback: z.union([z.string(), z.number(), z.null()]),
  requiresRestart: z.boolean(),
});
export type TunableState = z.infer<typeof tunableStateSchema>;

// ── TOGGLE (bramka workera) ─────────────────────────────────────────────────────

export const toggleStateSchema = z.object({
  enabled: z.boolean(),
  dryRun: z.boolean().optional(),
  destructive: z.boolean().optional(),
});
export type ToggleState = z.infer<typeof toggleStateSchema>;

// ── Grupy ────────────────────────────────────────────────────────────────────────

export const circleDmAiGroupSchema = z.object({
  claudeMaxConcurrent: tunableStateSchema,
  pollingIntervalMs: tunableStateSchema,
  voiceTranscriptIntervalMs: tunableStateSchema,
  imageDescriptionIntervalMs: tunableStateSchema,
  kbBudgetTokens: tunableStateSchema,
  kbHardCeilingTokens: tunableStateSchema,
  openaiWhisperModel: tunableStateSchema,
  openaiVisionModel: tunableStateSchema,
});
export type CircleDmAiGroup = z.infer<typeof circleDmAiGroupSchema>;

export const membershipGroupSchema = z.object({
  cleanup: toggleStateSchema,
  klarnaReconcile: toggleStateSchema,
  inviteRetry: toggleStateSchema,
  cleanupIntervalMs: tunableStateSchema,
  klarnaReconcileIntervalMs: tunableStateSchema,
  inviteRetryIntervalMs: tunableStateSchema,
});
export type MembershipGroup = z.infer<typeof membershipGroupSchema>;

export const billingNewsletterGroupSchema = z.object({
  frontendUrl: tunableStateSchema,
  confirmUrlBase: tunableStateSchema,
  cancellationFromEmail: tunableStateSchema,
  newsletterFromEmail: tunableStateSchema,
  senderGroupIds: tunableStateSchema,
  ebookFilePath: tunableStateSchema,
});
export type BillingNewsletterGroup = z.infer<typeof billingNewsletterGroupSchema>;

export const analyticsGroupSchema = z.object({
  metaPixelId: tunableStateSchema,
  circleCommunityId: tunableStateSchema,
});
export type AnalyticsGroup = z.infer<typeof analyticsGroupSchema>;

export const adminSettingsSchema = z.object({
  groups: z.object({
    circleDmAi: circleDmAiGroupSchema,
    membership: membershipGroupSchema,
    billingNewsletter: billingNewsletterGroupSchema,
    analytics: analyticsGroupSchema,
  }),
});
export type AdminSettings = z.infer<typeof adminSettingsSchema>;

export type SettingsGroupName = keyof AdminSettings['groups'];

// ── Patche PUT (camelCase, częściowy patch jednej grupy) ────────────────────────

/** TUNABLE: { value: scalar|null }. null przywraca fallback env. */
export type TunablePatch = { value: string | number | null };
/** TOGGLE: { enabled, dryRun? }. */
export type TogglePatch = { enabled: boolean; dryRun?: boolean };

export type CircleDmAiPatch = Partial<Record<keyof CircleDmAiGroup, TunablePatch>>;
export type MembershipPatch = {
  cleanup?: TogglePatch;
  klarnaReconcile?: TogglePatch;
  inviteRetry?: TogglePatch;
  cleanupIntervalMs?: TunablePatch;
  klarnaReconcileIntervalMs?: TunablePatch;
  inviteRetryIntervalMs?: TunablePatch;
};
export type BillingNewsletterPatch = Partial<Record<keyof BillingNewsletterGroup, TunablePatch>>;
export type AnalyticsPatch = Partial<Record<keyof AnalyticsGroup, TunablePatch>>;

// ── Połączenia API (SECRET - status-only) ───────────────────────────────────────

export const connectionStatusSchema = z.enum(['ok', 'error', 'unconfigured', 'skipped', 'mock']);
export type ConnectionStatus = z.infer<typeof connectionStatusSchema>;

/** Źródło efektywnej wartości klucza: ustawiony w panelu, z env, albo brak. */
export const connectionSourceSchema = z.enum(['panel', 'env', 'brak']);
export type ConnectionSource = z.infer<typeof connectionSourceSchema>;

export const connectionResultSchema = z.object({
  key: z.string(),
  label: z.string(),
  configured: z.boolean(),
  status: connectionStatusSchema,
  detail: z.string(),
  source: connectionSourceSchema,
  /** Czy klucz da się ustawić w panelu (4 sekrety) czy tylko status z env. */
  editable: z.boolean(),
  /** Zamaskowana EFEKTYWNA wartość (tylko editable). Nigdy pełna wartość. */
  masked: z.string().nullable(),
});
export type ConnectionResult = z.infer<typeof connectionResultSchema>;

export const connectionsResponseSchema = z.object({
  connections: z.array(connectionResultSchema),
});
export type ConnectionsResponse = z.infer<typeof connectionsResponseSchema>;

/** Body PUT /api/admin/connections/{key}/secret - ustawienie klucza. */
export type ConnectionSecretBody = { value: string };
/** Odpowiedź GET /api/admin/connections/{key}/secret/reveal - pełna wartość. */
export type ConnectionSecretReveal = { value: string };

// ── Ręczny przebieg cleanupu (POST .../workers/membership_cleanup/run) ──────────

export const cleanupRunResultSchema = z.object({
  success: z.boolean(),
  checked: z.number().int(),
  removed: z.number().int(),
  wouldRemove: z.number().int(),
  dryRun: z.boolean(),
  decisions: z.array(
    z.object({
      memberId: z.number().int().nullable(),
      email: z.string().nullable(),
      decision: z.string(),
      removed: z.boolean(),
    }),
  ),
});
export type CleanupRunResult = z.infer<typeof cleanupRunResultSchema>;
