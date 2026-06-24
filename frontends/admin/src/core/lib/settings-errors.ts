/**
 * Inteligentna warstwa błędów sekcji Ustawienia. Tłumaczy surowe komunikaty z
 * backendu (walidacja settings_catalog.py, testy połączeń connections.py) na
 * podpowiedzi głosem marki: co się stało i co z tym zrobić.
 *
 * Stringi match[] biorę z prawdziwego kodu backendu:
 * - walidacja: app/modules/admin/services/settings_catalog.py
 *   ("expected integer", "must be >= ...", "must be <= ...",
 *    "must be a valid URL", "expected non-empty string", "expected string",
 *    "unknown key: ...", "empty patch", "expected object", "missing 'value'",
 *    "'enabled' must be boolean", "'dryRun' must be boolean")
 * - połączenia: app/modules/admin/services/connections.py
 *   ("brak klucza w env", "HTTP <kod>", "timeout", "blad sieci: ...",
 *    "test padl: ...", "brak CIRCLE_API_TOKEN / CIRCLE_COMMUNITY_ID",
 *    "brak OPENAI_API_KEY", "brak RESEND_API_KEY")
 */
import { ApiError } from '@/core/lib/settings-api';

export interface SmartError {
  title: string;
  hint: string;
  /** Surowy techniczny detal (oryginalny komunikat backendu) - pod hintem, mniejszy. */
  rawDetail?: string;
  /** Wewnętrzna trasa do akcji naprawczej (np. /ustawienia/connections). */
  actionRoute?: string;
  actionLabel?: string;
}

const CONNECTIONS_ROUTE = '/ustawienia/connections';

type Matcher = {
  /** Pasuje, gdy status HTTP jest jednym z (pusta lista = każdy status). */
  status?: number[];
  /** Pasuje, gdy komunikat zawiera DOWOLNY z tych fragmentów (lowercase). */
  contains: string[];
  title: string;
  hint: string;
  actionRoute?: string;
  actionLabel?: string;
};

/**
 * Lista dopasowań po STATUSIE + fragmencie komunikatu (nie po całym stringu).
 * Pierwszy trafiony wygrywa, dlatego specyficzne idą przed ogólnymi.
 */
export const ERROR_HINTS: Matcher[] = [
  // ── Walidacja knobów (PUT settings -> 400) ──────────────────────────────
  {
    status: [400],
    contains: ['must be a valid url'],
    title: 'To nie wygląda na adres',
    hint: 'Wpisz pełny URL z protokołem, np. https://befreeclub.pl. Bez "https://" backend to odrzuci.',
  },
  {
    status: [400],
    contains: ['expected integer'],
    title: 'Tu wchodzi tylko liczba',
    hint: 'Wpisz samą liczbę, bez liter i spacji.',
  },
  {
    status: [400],
    contains: ['must be >='],
    title: 'Wartość za niska',
    hint: 'Interwały trzymaj od 5000 ms w górę, współbieżność Claude od 1. Backend pilnuje dolnej granicy.',
  },
  {
    status: [400],
    contains: ['must be <='],
    title: 'Wartość za wysoka',
    hint: 'Max równoległych zapytań Claude to 8. Wpisz mniej.',
  },
  {
    status: [400],
    contains: ['expected non-empty string', 'expected string'],
    title: 'Pole nie może być puste',
    hint: 'Wpisz wartość albo wyczyść pole i zapisz - puste przywraca wartość z env.',
  },
  {
    status: [400],
    contains: ['must be boolean'],
    title: 'Przełącznik przyjmuje tylko wł/wył',
    hint: 'Odśwież stronę i przełącz jeszcze raz.',
  },
  {
    status: [400],
    contains: ['unknown key', 'empty patch', 'expected object', "missing 'value'"],
    title: 'Czegoś nie udało się zapisać',
    hint: 'Odśwież stronę i spróbuj raz jeszcze. Jak wraca, zajrzyj w logi serwera.',
  },
  // ── Sekrety / połączenia (zwykle przez resolveConnectionError, ale i tu) ──
  {
    contains: ['brak klucza w env', 'brak openai_api_key', 'brak resend_api_key'],
    title: 'Brak klucza w env',
    hint: 'Ustaw klucz w env na serwerze. Sekret nie idzie przez panel.',
    actionRoute: CONNECTIONS_ROUTE,
    actionLabel: 'Zobacz Połączenia API',
  },
  {
    contains: ['brak circle_api_token', 'circle_community_id'],
    title: 'Circle nie jest podpięte',
    hint: 'Brakuje tokenu Circle (env) albo community ID. Token ustaw w env, community ID w Analityce.',
    actionRoute: CONNECTIONS_ROUTE,
    actionLabel: 'Zobacz Połączenia API',
  },
  // ── Sesja / uprawnienia ──────────────────────────────────────────────────
  {
    status: [403],
    contains: [],
    title: 'Brak uprawnień',
    hint: 'To konto nie ma dostępu do tej akcji. Zaloguj się jako admin.',
  },
  {
    status: [404],
    contains: [],
    title: 'Nie znaleziono',
    hint: 'Zasób zniknął albo zmienił adres. Odśwież stronę.',
  },
];

function messageOf(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return String(err);
}

function statusOf(err: unknown): number | undefined {
  return err instanceof ApiError ? err.status : undefined;
}

/**
 * Mapuje błąd (zwykle ApiError) na podpowiedź. Zawsze coś zwraca: gdy nic nie
 * pasuje, pokazuje kod błędu i prosi o ponowienie. NIGDY nie pokazuje gołego
 * stacktrace jako głównej treści - oryginał ląduje w rawDetail.
 */
export function resolveSmartError(err: unknown): SmartError {
  const status = statusOf(err);
  const raw = messageOf(err);
  const folded = raw.toLowerCase();

  for (const m of ERROR_HINTS) {
    if (m.status && (status === undefined || !m.status.includes(status))) continue;
    if (m.contains.length > 0 && !m.contains.some((frag) => folded.includes(frag))) continue;
    return {
      title: m.title,
      hint: m.hint,
      rawDetail: raw !== m.hint ? raw : undefined,
      actionRoute: m.actionRoute,
      actionLabel: m.actionLabel,
    };
  }

  return {
    title: 'Coś poszło nie tak',
    hint: status
      ? `Kod błędu: ${status}. Spróbuj ponownie, a jak wraca, zajrzyj w logi serwera.`
      : 'Spróbuj ponownie, a jak wraca, zajrzyj w logi serwera.',
    rawDetail: raw,
  };
}

/**
 * Podpowiedź dla testu połączenia. Test wraca HTTP 200 ze status='error' /
 * 'unconfigured' (NIE rzuca), więc dostaje key + status + detail z backendu.
 * Zwraca null, gdy nie ma o czym mówić (ok/skipped/mock).
 */
export function resolveConnectionError(
  key: string,
  status: string,
  detail: string,
): SmartError | null {
  if (status === 'ok' || status === 'skipped' || status === 'mock') return null;

  if (status === 'unconfigured') {
    return {
      title: 'Brak klucza w env',
      hint: 'Ustaw klucz w env na serwerze i zrestartuj backend. Sekret nie idzie przez panel.',
      rawDetail: detail || undefined,
    };
  }

  // status === 'error': test się odpalił, ale padł. Detal niesie realny powód.
  const folded = (detail || '').toLowerCase();

  if (key === 'circle' && (folded.includes('http 401') || folded.includes('http 403'))) {
    return {
      title: 'Circle odrzucił token',
      hint: 'Token Circle jest nieważny albo nie ma uprawnień. Wygeneruj nowy w env, zachowaj community ID.',
      rawDetail: detail,
    };
  }
  if (folded.includes('http 401') || folded.includes('http 403')) {
    return {
      title: 'Klucz nie został przyjęty',
      hint: 'Dostawca odrzucił klucz (401/403). Sprawdź, czy wartość w env jest aktualna.',
      rawDetail: detail,
    };
  }
  if (folded.includes('timeout')) {
    return {
      title: 'Brak odpowiedzi w czasie',
      hint: 'Dostawca nie odpowiedział w 8 s. Może być chwilowo niedostępny. Spróbuj ponownie.',
      rawDetail: detail,
    };
  }
  if (folded.includes('blad sieci')) {
    return {
      title: 'Nie udało się połączyć',
      hint: 'Backend nie dobił do dostawcy. Sprawdź sieć kontenera i status usługi.',
      rawDetail: detail,
    };
  }

  return {
    title: 'Test połączenia padł',
    hint: 'Coś nie zadziałało po stronie dostawcy. Szczegóły niżej, w razie czego zajrzyj w logi.',
    rawDetail: detail || undefined,
  };
}
