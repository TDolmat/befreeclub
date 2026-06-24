/**
 * JEDNO źródło prawdy sekcji Ustawienia: kategorie + pola + metadane wyszukiwania.
 *
 * Labelki i opisy PL żyją TU (backendowy katalog ma tylko deskryptory techniczne).
 * Źródła tekstu: docs/spec-landing/ustawienia-katalog.md + obecne sekcje UI.
 *
 * Konwencja fieldId: camelCase klucz pola z @bfc/shared (np. claudeMaxConcurrent).
 * Ten sam string jest DOM id wiersza (SettingsRow id={fieldId}) -> deep-link
 * ?focus=fieldId. keywords[] każdego pola zawierają: camelCase fieldId, klucz
 * admin.settings (np. circle_dm.claude_max_concurrent) i nazwę env
 * (np. CLAUDE_MAX_CONCURRENT), żeby search trafiał niezależnie od nazewnictwa.
 *
 * Connections to kategoria status-only: pola = integracje, keyword = nazwa env
 * sekretu, kind 'status'. Nigdy nie ma tu edytowalnej wartości sekretu.
 */
import { BarChart3, Bot, type LucideIcon, Mail, Plug, Users } from 'lucide-react';

export type FieldKind = 'tunable' | 'toggle' | 'status' | 'link';

export interface SettingsField {
  /** camelCase klucz @bfc/shared = DOM id wiersza (deep-link). */
  fieldId: string;
  label: string;
  description: string;
  keywords: string[];
  kind: FieldKind;
}

export type SettingsCategoryId =
  | 'membership'
  | 'circleDmAi'
  | 'billingNewsletter'
  | 'analytics'
  | 'connections';

export interface SettingsCategory {
  id: SettingsCategoryId;
  route: string;
  label: string;
  description: string;
  icon: LucideIcon;
  fields: SettingsField[];
}

/** Kolejność wg DECYZJI #6. */
export const SETTINGS_CATEGORIES: SettingsCategory[] = [
  {
    id: 'membership',
    route: '/ustawienia/membership',
    label: 'Członkostwa',
    description:
      'Workery, które mogą ruszyć realnych ludzi w Circle. Świeży deploy nikogo nie usuwa: wszystko startuje wyłączone, cleanup dodatkowo w trybie cienia.',
    icon: Users,
    fields: [
      {
        fieldId: 'cleanup',
        label: 'Automatyczny cleanup członkostw',
        description:
          'Usuwa z Circle ludzi bez aktywnej subskrypcji. Destrukcyjny. Włączenie realnego usuwania za potwierdzeniem.',
        keywords: [
          'cleanup',
          'members.cleanup',
          'usuwanie',
          'czlonkostwa',
          'subskrypcja',
          'circle',
        ],
        kind: 'toggle',
      },
      {
        fieldId: 'klarnaReconcile',
        label: 'Automatyczny reconcile Klarny',
        description: 'Nadaje dostęp po opłaceniu Klarną. Nie usuwa. Domyślnie wyłączony.',
        keywords: ['klarnaReconcile', 'members.klarna_reconcile', 'klarna', 'reconcile', 'dostep'],
        kind: 'toggle',
      },
      {
        fieldId: 'inviteRetry',
        label: 'Automatyczne ponawianie zaproszeń Circle',
        description: 'Ponawia tylko nieudane zaproszenia do Circle. Domyślnie wyłączony.',
        keywords: ['inviteRetry', 'members.invite_retry', 'zaproszenia', 'invite', 'retry'],
        kind: 'toggle',
      },
      {
        fieldId: 'cleanupIntervalMs',
        label: 'Interwał cleanupu (ms)',
        description: 'Co ile worker cleanupu robi przebieg. Zmiana zadziała po restarcie backendu.',
        keywords: [
          'cleanupIntervalMs',
          'members.cleanup_interval_ms',
          'MEMBERSHIP_CLEANUP_INTERVAL_MS',
          'interwal',
          'cleanup',
        ],
        kind: 'tunable',
      },
      {
        fieldId: 'klarnaReconcileIntervalMs',
        label: 'Interwał reconcile Klarny (ms)',
        description: 'Co ile worker reconcile Klarny robi przebieg. Po restarcie backendu.',
        keywords: [
          'klarnaReconcileIntervalMs',
          'members.klarna_reconcile_interval_ms',
          'KLARNA_RECONCILE_INTERVAL_MS',
          'interwal',
          'klarna',
        ],
        kind: 'tunable',
      },
      {
        fieldId: 'inviteRetryIntervalMs',
        label: 'Interwał ponawiania zaproszeń (ms)',
        description: 'Co ile worker ponawia nieudane zaproszenia. Po restarcie backendu.',
        keywords: [
          'inviteRetryIntervalMs',
          'members.invite_retry_interval_ms',
          'INVITE_RETRY_INTERVAL_MS',
          'interwal',
          'zaproszenia',
        ],
        kind: 'tunable',
      },
    ],
  },
  {
    id: 'circleDmAi',
    route: '/ustawienia/circleDmAi',
    label: 'Circle DM & AI',
    description:
      'Współbieżność, polling, budżety bazy wiedzy i modele mediów. Prompty, modele draft/format i progi wątków masz na osobnej stronie ustawień Circle DM.',
    icon: Bot,
    fields: [
      {
        fieldId: 'claudeMaxConcurrent',
        label: 'Max równoległych zapytań Claude',
        description: '1-8. Semafor tworzony przy starcie, zmiana zadziała po restarcie backendu.',
        keywords: [
          'claudeMaxConcurrent',
          'circle_dm.claude_max_concurrent',
          'CLAUDE_MAX_CONCURRENT',
          'claude',
          'semafor',
          'wspolbieznosc',
          'concurrent',
        ],
        kind: 'tunable',
      },
      {
        fieldId: 'pollingIntervalMs',
        label: 'Interwał pollingu Circle (ms)',
        description: 'Co ile odpytujemy Circle o nowe wiadomości. Po restarcie backendu.',
        keywords: [
          'pollingIntervalMs',
          'circle_dm.polling_interval_ms',
          'POLLING_INTERVAL_MS',
          'polling',
          'interwal',
        ],
        kind: 'tunable',
      },
      {
        fieldId: 'voiceTranscriptIntervalMs',
        label: 'Interwał transkrypcji głosówek (ms)',
        description: 'Co ile worker transkrybuje głosówki. Po restarcie backendu.',
        keywords: [
          'voiceTranscriptIntervalMs',
          'circle_dm.voice_transcript_interval_ms',
          'VOICE_TRANSCRIPT_INTERVAL_MS',
          'transkrypcja',
          'glosowki',
          'voice',
        ],
        kind: 'tunable',
      },
      {
        fieldId: 'imageDescriptionIntervalMs',
        label: 'Interwał opisu obrazków (ms)',
        description: 'Co ile worker opisuje obrazki. Po restarcie backendu.',
        keywords: [
          'imageDescriptionIntervalMs',
          'circle_dm.image_description_interval_ms',
          'IMAGE_DESCRIPTION_INTERVAL_MS',
          'obrazki',
          'vision',
          'opis',
        ],
        kind: 'tunable',
      },
      {
        fieldId: 'kbBudgetTokens',
        label: 'Budżet tokenów bazy wiedzy',
        description: 'Miękki budżet tokenów na kontekst z bazy wiedzy. Działa bez restartu.',
        keywords: [
          'kbBudgetTokens',
          'circle_dm.kb_budget_tokens',
          'KB_BUDGET_TOKENS',
          'baza wiedzy',
          'tokeny',
          'budzet',
        ],
        kind: 'tunable',
      },
      {
        fieldId: 'kbHardCeilingTokens',
        label: 'Twardy limit tokenów bazy wiedzy',
        description: 'Górny limit tokenów z bazy wiedzy. Działa bez restartu.',
        keywords: [
          'kbHardCeilingTokens',
          'circle_dm.kb_hard_ceiling_tokens',
          'KB_HARD_CEILING_TOKENS',
          'baza wiedzy',
          'tokeny',
          'limit',
        ],
        kind: 'tunable',
      },
      {
        fieldId: 'openaiWhisperModel',
        label: 'Model transkrypcji (Whisper)',
        description: 'Model OpenAI do transkrypcji głosówek. Czytany per użycie.',
        keywords: [
          'openaiWhisperModel',
          'circle_dm.openai_whisper_model',
          'OPENAI_WHISPER_MODEL',
          'whisper',
          'transkrypcja',
          'stt',
        ],
        kind: 'tunable',
      },
      {
        fieldId: 'openaiVisionModel',
        label: 'Model opisu obrazków (vision)',
        description: 'Model OpenAI do opisu obrazków. Czytany per użycie.',
        keywords: [
          'openaiVisionModel',
          'circle_dm.openai_vision_model',
          'OPENAI_VISION_MODEL',
          'vision',
          'obrazki',
          'gpt',
        ],
        kind: 'tunable',
      },
    ],
  },
  {
    id: 'billingNewsletter',
    route: '/ustawienia/billingNewsletter',
    label: 'Billing & Newsletter',
    description: 'Adresy w mailach, nadawcy i grupy Sender. Klucze API są w sekcji Połączenia.',
    icon: Mail,
    fields: [
      {
        fieldId: 'frontendUrl',
        label: 'Adres frontu (linki w mailach)',
        description: 'Bazowy URL do linków i redirectów. Współdzielony przez billing i newsletter.',
        keywords: ['frontendUrl', 'billing.frontend_url', 'FRONTEND_URL', 'url', 'front', 'linki'],
        kind: 'tunable',
      },
      {
        fieldId: 'confirmUrlBase',
        label: 'Adres potwierdzenia newslettera',
        description: 'Bazowy URL strony potwierdzenia double opt-in newslettera.',
        keywords: [
          'confirmUrlBase',
          'newsletter.confirm_url_base',
          'CONFIRM_URL_BASE',
          'potwierdzenie',
          'newsletter',
          'doi',
        ],
        kind: 'tunable',
      },
      {
        fieldId: 'cancellationFromEmail',
        label: 'Nadawca maili (anulowanie/karta)',
        description: 'Adres nadawcy maili o anulowaniu i zmianie karty. Format: Nazwa <mail>.',
        keywords: [
          'cancellationFromEmail',
          'billing.cancellation_from_email',
          'CANCELLATION_FROM_EMAIL',
          'nadawca',
          'email',
          'anulowanie',
        ],
        kind: 'tunable',
      },
      {
        fieldId: 'newsletterFromEmail',
        label: 'Nadawca maili newslettera',
        description: 'Adres nadawcy newslettera. Format: Nazwa <mail>.',
        keywords: [
          'newsletterFromEmail',
          'newsletter.from_email',
          'NEWSLETTER_FROM_EMAIL',
          'nadawca',
          'newsletter',
          'email',
        ],
        kind: 'tunable',
      },
      {
        fieldId: 'senderGroupIds',
        label: 'Grupy Sender.net (id, po przecinku)',
        description: 'Id grup w Sender.net, do których trafia zapis newslettera. CSV.',
        keywords: [
          'senderGroupIds',
          'newsletter.sender_group_ids',
          'SENDER_GROUP_IDS',
          'sender',
          'grupy',
          'newsletter',
        ],
        kind: 'tunable',
      },
      {
        fieldId: 'ebookFilePath',
        label: 'Ścieżka pliku ebooka (na dysku VPS)',
        description: 'Plik PDF ebooka na dysku hosta. Puste = ebook niedostępny.',
        keywords: ['ebookFilePath', 'billing.ebook_file_path', 'EBOOK_FILE_PATH', 'ebook', 'pdf'],
        kind: 'tunable',
      },
    ],
  },
  {
    id: 'analytics',
    route: '/ustawienia/analytics',
    label: 'Analityka',
    description: 'Parametry niesekretne. Token Meta Conversions API jest w sekcji Połączenia.',
    icon: BarChart3,
    fields: [
      {
        fieldId: 'metaPixelId',
        label: 'Meta Pixel ID',
        description: 'Bez Pixel ID i tokenu CAPI nie strzelamy zdarzeń do Meta.',
        keywords: [
          'metaPixelId',
          'analytics.meta_pixel_id',
          'META_PIXEL_ID',
          'meta',
          'pixel',
          'capi',
        ],
        kind: 'tunable',
      },
      {
        fieldId: 'circleCommunityId',
        label: 'Circle community ID',
        description: 'Id społeczności Circle. Para z tokenem Circle decyduje o połączeniu.',
        keywords: [
          'circleCommunityId',
          'members.circle_community_id',
          'CIRCLE_COMMUNITY_ID',
          'circle',
          'community',
        ],
        kind: 'tunable',
      },
    ],
  },
  {
    id: 'connections',
    route: '/ustawienia/connections',
    label: 'Połączenia API',
    description:
      'Status kluczy do zewnętrznych usług. Klucze ustawiasz w pliku .env na serwerze, nie tutaj. Panel widzi tylko, czy są ustawione i czy działa test.',
    icon: Plug,
    fields: [
      {
        fieldId: 'stripeCurrent',
        label: 'Stripe (konto current)',
        description: 'Główne konto Stripe. Status z env, test robi GET /v1/balance.',
        keywords: ['stripeCurrent', 'STRIPE_SECRET_KEY', 'stripe', 'platnosci', 'current'],
        kind: 'status',
      },
      {
        fieldId: 'stripeLegacy',
        label: 'Stripe (konto legacy)',
        description: 'Stare konto Stripe. Status z env, test robi GET /v1/balance.',
        keywords: ['stripeLegacy', 'STRIPE_LEGACY_SECRET_KEY', 'stripe', 'legacy'],
        kind: 'status',
      },
      {
        fieldId: 'circle',
        label: 'Circle API',
        description: 'Token Circle z env + community ID. Test pobiera 1 członka.',
        keywords: ['circle', 'CIRCLE_API_TOKEN', 'circle', 'api', 'czlonkowie'],
        kind: 'status',
      },
      {
        fieldId: 'openai',
        label: 'OpenAI (STT/vision)',
        description: 'Klucz OpenAI do transkrypcji i opisu obrazków. Test: GET /v1/models.',
        keywords: ['openai', 'OPENAI_API_KEY', 'openai', 'whisper', 'vision'],
        kind: 'status',
      },
      {
        fieldId: 'resend',
        label: 'Resend (maile)',
        description: 'Klucz Resend do wysyłki maili. Test: GET /domains.',
        keywords: ['resend', 'RESEND_API_KEY', 'resend', 'maile', 'email'],
        kind: 'status',
      },
      {
        fieldId: 'sender',
        label: 'Sender.net (newsletter)',
        description: 'Token Sender.net. Bez taniego testu - sam status z env.',
        keywords: ['sender', 'SENDER_API_TOKEN', 'sender', 'newsletter'],
        kind: 'status',
      },
      {
        fieldId: 'metaCapi',
        label: 'Meta Conversions API',
        description: 'Token Meta CAPI + Pixel ID. Bez taniego testu - sam status z env.',
        keywords: ['metaCapi', 'META_CAPI_TOKEN', 'meta', 'capi', 'conversions'],
        kind: 'status',
      },
    ],
  },
];

export interface SearchIndexEntry extends SettingsField {
  categoryId: SettingsCategoryId;
  categoryLabel: string;
  route: string;
}

/** Płaski indeks: każde pole + skąd jest, do globalnego searcha ustawień. */
export const SETTINGS_SEARCH_INDEX: SearchIndexEntry[] = SETTINGS_CATEGORIES.flatMap((cat) =>
  cat.fields.map((field) => ({
    ...field,
    categoryId: cat.id,
    categoryLabel: cat.label,
    route: cat.route,
  })),
);

/** Pomocnik: kategoria po id (np. z useParams). */
export function getCategory(id: string): SettingsCategory | undefined {
  return SETTINGS_CATEGORIES.find((c) => c.id === id);
}
