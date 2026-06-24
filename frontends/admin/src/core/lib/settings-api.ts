/**
 * Centralna sekcja Ustawienia panelu admina.
 * - GET/PUT  /api/admin/settings
 * - GET      /api/admin/connections (?test=1), POST /api/admin/connections/{key}/test
 * - POST     /api/billing/admin/workers/membership_cleanup/run (ręczny cleanup)
 *
 * Cookie-based session, same-origin. 401 -> reload (App.tsx pokaże login).
 * 4 klucze API (openai/resend/sender/metaCapi) są edytowalne: PUT/DELETE secret +
 * reveal pełnej wartości na żądanie. Pełnej wartości NIGDY nie trzymamy w stanie
 * globalnym - reveal woła się ad hoc i ląduje tylko w lokalnym useState wiersza.
 */
import type {
  AdminSettings,
  AnalyticsPatch,
  BillingNewsletterPatch,
  CircleDmAiPatch,
  CleanupRunResult,
  ConnectionResult,
  ConnectionSecretReveal,
  ConnectionsResponse,
  MembershipPatch,
} from '@bfc/shared';

/**
 * Błąd z API ustawień. Niesie status HTTP + sparsowane ciało ({error?} albo
 * surowy tekst), żeby resolveSmartError mógł dopasować podpowiedź po statusie i
 * fragmencie komunikatu. message zostaje czytelny (kompat z (err as Error).message).
 */
export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, message: string, body: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

async function http<T>(
  method: 'GET' | 'POST' | 'PUT' | 'DELETE',
  path: string,
  body?: unknown,
): Promise<T> {
  const res = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  if (!res.ok) {
    // Quirk zachowany 1:1: 401 = sesja padła, przeładuj (App.tsx pokaże login) PRZED throw.
    if (res.status === 401) window.location.reload();
    let message = `${res.status} ${res.statusText}`;
    let parsedBody: unknown = text;
    try {
      const parsed = JSON.parse(text);
      parsedBody = parsed;
      if (parsed && typeof parsed.error === 'string') message = parsed.error;
    } catch {
      if (text) message = text;
    }
    throw new ApiError(res.status, message, parsedBody);
  }
  return text ? (JSON.parse(text) as T) : (undefined as T);
}

type GroupPatch = {
  circleDmAi: CircleDmAiPatch;
  membership: MembershipPatch;
  billingNewsletter: BillingNewsletterPatch;
  analytics: AnalyticsPatch;
};

export const settingsApi = {
  get: () => http<AdminSettings>('GET', '/api/admin/settings'),
  update: <G extends keyof GroupPatch>(group: G, patch: GroupPatch[G]) =>
    http<Record<string, unknown>>('PUT', `/api/admin/settings/${group}`, patch),

  connections: (runTests = false) =>
    http<ConnectionsResponse>('GET', `/api/admin/connections${runTests ? '?test=1' : ''}`),
  testConnection: (key: string) =>
    http<{ connection: ConnectionResult }>('POST', `/api/admin/connections/${key}/test`),

  // ── Sekrety 4 edytowalnych integracji (tylko editable=true) ──────────────
  /** Ustaw nowy klucz. Odpowiedź = świeży status z masked, BEZ pełnej wartości. */
  setConnectionSecret: (key: string, value: string) =>
    http<{ connection: ConnectionResult }>('PUT', `/api/admin/connections/${key}/secret`, {
      value,
    }),
  /** Wyczyść klucz z panelu (powrót do env). */
  clearConnectionSecret: (key: string) =>
    http<{ connection: ConnectionResult }>('DELETE', `/api/admin/connections/${key}/secret`),
  /** Pełna efektywna wartość na żądanie (przycisk Pokaż). Nie cache'ować. */
  revealConnectionSecret: (key: string) =>
    http<ConnectionSecretReveal>('GET', `/api/admin/connections/${key}/secret/reveal`),

  runCleanup: () =>
    http<CleanupRunResult>('POST', '/api/billing/admin/workers/membership_cleanup/run'),
};
