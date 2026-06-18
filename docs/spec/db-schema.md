# Spec: schemat bazy danych Postgres (panel admina BFC)

Specyfikacja 1:1 do portu backendu z Hono+Drizzle na FastAPI. Frontend React zostaje, więc nazwy tabel, kolumn, enumów, constraintów i zachowanie triggerów muszą zostać identyczne.

Źródła (stan na dziś, po migracji 0014):

- `/Users/tomasz/repos/befreeclub/admin/apps/server/src/core/db/schema.ts` (definicja Drizzle, stan końcowy)
- `/Users/tomasz/repos/befreeclub/admin/apps/server/src/core/db/index.ts` (połączenie)
- `/Users/tomasz/repos/befreeclub/admin/apps/server/src/core/db/migrate.ts` (runner migracji + triggery)
- `/Users/tomasz/repos/befreeclub/admin/apps/server/src/core/db/migrations/0000-0014*.sql` (15 migracji)
- `/Users/tomasz/repos/befreeclub/admin/apps/server/drizzle.config.ts`

Baza: Postgres 16, wszystko w schemacie `public` (enumy tworzone jako `"public"."nazwa"`).

---

## 1. Połączenie z bazą (index.ts)

Klient: `postgres` (postgres-js) + Drizzle.

```ts
const queryClient = postgres(env.DATABASE_URL, {
  max: 10,          // pool: max 10 połączeń
  idle_timeout: 30, // sekundy
  prepare: true,    // prepared statements włączone
});
export const db = drizzle(queryClient, { schema, casing: 'snake_case' });
export async function closeDb() { await queryClient.end({ timeout: 5 }); } // graceful close, 5 s
```

- `DATABASE_URL` przychodzi z walidowanego env (`../env.js`).
- `casing: 'snake_case'` jest ustawione, ale i tak KAŻDA kolumna w `schema.ts` ma jawnie podaną nazwę snake_case, więc mapowanie jest deterministyczne (pełna tabela mapowań w sekcji 7).
- `closeDb()` z timeoutem 5 s wywoływany przy shutdownie serwera.

## 2. Mechanika migracji (migrate.ts + drizzle.config.ts)

`drizzle.config.ts`:

```ts
export default defineConfig({
  schema: './src/core/db/schema.ts',
  out: './src/core/db/migrations',
  dialect: 'postgresql',
  dbCredentials: { url },   // process.env.DATABASE_URL, throw jeśli brak
  strict: true,
  verbose: true,
});
```

`migrate.ts` (uruchamiany przez `pnpm db:migrate`, w prod z `dist/core/db/migrations/`):

1. Loguje `▶ Running migrations against <DATABASE_URL z zamaskowanym hasłem>` - maskowanie regexem `url.replace(/:[^:@]+@/, ':***@')`.
2. Osobne połączenie `postgres(env.DATABASE_URL, { max: 1 })`.
3. `migrate(dbInstance, { migrationsFolder })` - drizzle-orm/postgres-js/migrator. Stan zaaplikowanych migracji trzyma w tabeli **`drizzle.__drizzle_migrations`** (schemat `drizzle`, kolumny: `id serial`, `hash text`, `created_at bigint`). Kolejność i tagi migracji z `migrations/meta/_journal.json` (idx 0-14).
4. PO migracjach instaluje funkcję i triggery `updated_at` (idempotentnie, `CREATE OR REPLACE` + `DROP TRIGGER IF EXISTS`). Dosłowna treść w sekcji 8. To znaczy: **triggery NIE są w plikach .sql migracji**, żyją tylko w migrate.ts.
5. `console.log('✅ Migrations applied')`, `process.exit(0)`; przy błędzie `❌ Migration failed:` + `process.exit(1)`.

Uwaga: migracje `0003_app_settings` i `0007_seed_format_prompt` to ręczne pliki SQL (nie mają snapshotów w `meta/`), zawierają seedy i constraint CHECK którego nie ma w `schema.ts`.

## 3. Enumy (10 sztuk, wszystkie w schemacie public)

| Nazwa PG | Wartości (dokładnie, w tej kolejności) |
|---|---|
| `chat_room_kind` | `direct`, `group_chat` |
| `draft_status` | `idle`, `generating`, `has_draft`, `polishing`, `ready_to_send`, `sent`, `error` |
| `iteration_kind` | `initial`, `user_feedback`, `polish` |
| `thread_status` | `inbox`, `done` |
| `kb_scope` | `global`, `account` |
| `kb_source_kind` | `pdf`, `md`, `manual` |
| `assistant_msg_role` | `user`, `assistant` |
| `feedback_status` | `open`, `done` |
| `voice_transcript_status` | `pending`, `done`, `error` |
| `image_description_status` | `pending`, `done`, `error` |

## 4. Tabele (stan końcowy, 16 tabel)

Wszystkie `timestamp` to `timestamp with time zone` (timestamptz). `bigserial` = PK autoinkrement BIGINT. W Drizzle wszystkie bigint/bigserial mają `mode: 'number'` (JS number, w JSON serializowane jako liczba).

### 4.1 `auth_accounts` - konta logowania do panelu

Niezależne od `admin_accounts` (tamte to konta Circle.so do wysyłki DM). To lista osób uprawnionych do otwarcia panelu.

| Kolumna | Typ | NULL | Default | Uwagi |
|---|---|---|---|---|
| `id` | bigserial | NOT NULL | auto | PK |
| `email` | text | NOT NULL | - | UNIQUE constraint `auth_accounts_email_unique` |
| `password_hash` | text | NOT NULL | - | |
| `created_at` | timestamptz | NOT NULL | `now()` | |
| `updated_at` | timestamptz | NOT NULL | `now()` | **brak triggera!** aktualizacja w kodzie aplikacji |

### 4.2 `auth_sessions` - sesje panelu

| Kolumna | Typ | NULL | Default | Uwagi |
|---|---|---|---|---|
| `id` | text | NOT NULL | - | PK; 32-bajtowy losowy hex string = wartość cookie sesji |
| `auth_account_id` | bigint | NOT NULL | - | FK -> `auth_accounts.id` ON DELETE **cascade**, constraint `auth_sessions_auth_account_id_auth_accounts_id_fk` |
| `expires_at` | timestamptz | NOT NULL | - | |
| `last_seen_at` | timestamptz | NOT NULL | `now()` | |
| `ip_addr` | text | NULL | - | |
| `user_agent` | text | NULL | - | |
| `created_at` | timestamptz | NOT NULL | `now()` | |

Indeksy: `idx_auth_sessions_expires` btree(`expires_at`).

### 4.3 `admin_accounts` - konta adminów Circle.so (do wysyłki DM)

| Kolumna | Typ | NULL | Default | Uwagi |
|---|---|---|---|---|
| `id` | bigserial | NOT NULL | auto | PK |
| `label` | text | NOT NULL | - | |
| `email` | text | NOT NULL | - | brak UNIQUE |
| `circle_admin_token` | text | NOT NULL | - | |
| `circle_refresh_token` | text | NULL | - | |
| `circle_access_token` | text | NULL | - | |
| `circle_access_token_expires_at` | timestamptz | NULL | - | |
| `community_id` | bigint | NULL | - | |
| `community_member_id` | bigint | NULL | - | |
| `system_prompt` | text | NOT NULL | - | |
| `is_active` | boolean | NOT NULL | `true` | |
| `last_synced_at` | timestamptz | NULL | - | |
| `created_at` | timestamptz | NOT NULL | `now()` | |
| `updated_at` | timestamptz | NOT NULL | `now()` | trigger `set_admin_accounts_updated_at` |

Historia: kolumny `polish_system_prompt` (drop w 0005), `draft_model` i `polish_model` (drop w 0006) - przeniesione globalnie do `app_settings`. W świeżej bazie po pełnym przebiegu migracji NIE istnieją.

### 4.4 `dm_threads` - wątki DM z Circle

| Kolumna | Typ | NULL | Default | Uwagi |
|---|---|---|---|---|
| `id` | bigserial | NOT NULL | auto | PK |
| `admin_account_id` | bigint | NOT NULL | - | FK -> `admin_accounts.id` ON DELETE **cascade** (`dm_threads_admin_account_id_admin_accounts_id_fk`) |
| `circle_chat_room_id` | bigint | NOT NULL | - | |
| `circle_chat_room_uuid` | uuid | NOT NULL | - | |
| `chat_room_kind` | `chat_room_kind` (enum) | NOT NULL | - | |
| `chat_room_name` | text | NULL | - | |
| `other_participant_email` | text | NULL | - | |
| `other_participant_name` | text | NULL | - | |
| `other_participant_id` | bigint | NULL | - | |
| `other_participant_avatar_url` | text | NULL | - | |
| `unread_messages_count` | integer | NOT NULL | `0` | |
| `pinned_at` | timestamptz | NULL | - | |
| `status` | `thread_status` (enum) | NOT NULL | `'inbox'` | `'done'` = archiwum, ukrywa wątek z widoków inbox/cisza/flaga |
| `is_flagged` | boolean | NOT NULL | `false` | przekrojowy bool filtra "Flaga", niezależny od status |
| `last_message_at` | timestamptz | NULL | - | |
| `last_message_sender_id` | bigint | NULL | - | |
| `last_message_sender_is_me` | boolean | NOT NULL | `false` | |
| `last_message_preview` | text | NULL | - | |
| `raw_payload` | jsonb | NULL | - | |
| `messages_fetched_at` | timestamptz | NULL | - | |
| `fetched_at` | timestamptz | NOT NULL | `now()` | brak `updated_at` w tej tabeli |

Constraint UNIQUE: `uniq_account_room` UNIQUE(`admin_account_id`, `circle_chat_room_uuid`) - target ON CONFLICT przy upsertach syncu.

Indeksy:
- `idx_threads_last_msg` btree(`admin_account_id`, `last_message_at`)
- `idx_threads_unread` btree(`admin_account_id`, `unread_messages_count`)
- `idx_threads_pinned` btree(`admin_account_id`, `pinned_at`)
- `idx_threads_status` btree(`admin_account_id`, `status`)
- `idx_threads_flagged` btree(`admin_account_id`, `is_flagged`)

### 4.5 `thread_checkups` - follow-upy per wątek

Dozwolonych wiele wpisów pending na wątek (kampania "za 2d, 7d, 14d"). `done_at` = wykonane: auto przy wysyłce (wszystkie pending stają się done) albo ręcznie z UI.

| Kolumna | Typ | NULL | Default | Uwagi |
|---|---|---|---|---|
| `id` | bigserial | NOT NULL | auto | PK |
| `thread_id` | bigint | NOT NULL | - | FK -> `dm_threads.id` ON DELETE **cascade** (`thread_checkups_thread_id_dm_threads_id_fk`) |
| `due_at` | timestamptz | NOT NULL | - | |
| `note` | text | NULL | - | |
| `done_at` | timestamptz | NULL | - | NULL = pending |
| `created_at` | timestamptz | NOT NULL | `now()` | |

Indeksy: `idx_checkups_thread` btree(`thread_id`); `idx_checkups_pending_due` btree(`due_at`) - UWAGA: mimo nazwy to NIE jest indeks częściowy (brak `WHERE done_at IS NULL`), zwykły btree na samym `due_at`.

### 4.6 `dm_messages` - wiadomości z Circle (cache)

| Kolumna | Typ | NULL | Default | Uwagi |
|---|---|---|---|---|
| `id` | bigserial | NOT NULL | auto | PK |
| `thread_id` | bigint | NOT NULL | - | FK -> `dm_threads.id` ON DELETE **cascade** (`dm_messages_thread_id_dm_threads_id_fk`) |
| `circle_message_id` | bigint | NOT NULL | - | |
| `body` | text | NOT NULL | - | |
| `rich_text_body` | jsonb | NULL | - | |
| `sender_id` | bigint | NULL | - | |
| `sender_name` | text | NULL | - | |
| `sender_is_me` | boolean | NOT NULL | - | **bez defaultu** |
| `parent_message_id` | bigint | NULL | - | brak FK, to ID z Circle |
| `chat_thread_id` | bigint | NULL | - | brak FK, ID z Circle |
| `created_at` | timestamptz | NOT NULL | - | **bez defaultu** - czas z Circle |
| `edited_at` | timestamptz | NULL | - | |
| `fetched_at` | timestamptz | NOT NULL | `now()` | |
| `voice_transcript` | text | NULL | - | |
| `voice_transcript_status` | `voice_transcript_status` (enum) | NULL | - | NULL = nie jest wiadomością głosową |
| `voice_transcript_error` | text | NULL | - | |
| `voice_transcript_attempts` | integer | NOT NULL | `0` | |
| `voice_duration_sec` | integer | NULL | - | |
| `voice_transcribed_at` | timestamptz | NULL | - | |

Constraint UNIQUE: `uniq_thread_message` UNIQUE(`thread_id`, `circle_message_id`) - target ON CONFLICT przy syncu wiadomości.

Indeksy: `idx_messages_thread_created` btree(`thread_id`, `created_at`); `idx_messages_voice_status` btree(`voice_transcript_status`).

### 4.7 `message_image_descriptions` - opisy AI załączników graficznych

| Kolumna | Typ | NULL | Default | Uwagi |
|---|---|---|---|---|
| `id` | bigserial | NOT NULL | auto | PK |
| `message_id` | bigint | NOT NULL | - | FK -> `dm_messages.id` ON DELETE **cascade** (`message_image_descriptions_message_id_dm_messages_id_fk`) |
| `attachment_index` | integer | NOT NULL | - | indeks załącznika w wiadomości |
| `attachment_url` | text | NOT NULL | - | |
| `description` | text | NULL | - | |
| `status` | `image_description_status` (enum) | NOT NULL | `'pending'` | |
| `error` | text | NULL | - | |
| `attempts` | integer | NOT NULL | `0` | |
| `created_at` | timestamptz | NOT NULL | `now()` | |
| `described_at` | timestamptz | NULL | - | |

Constraint UNIQUE: `uniq_msg_image_idx` UNIQUE(`message_id`, `attachment_index`).
Indeks: `idx_image_desc_status` btree(`status`).

### 4.8 `draft_sessions` - sesje draftowania AI (1:1 z wątkiem)

| Kolumna | Typ | NULL | Default | Uwagi |
|---|---|---|---|---|
| `id` | bigserial | NOT NULL | auto | PK |
| `thread_id` | bigint | NOT NULL | - | UNIQUE (`draft_sessions_thread_id_unique`) + FK -> `dm_threads.id` ON DELETE **cascade** (`draft_sessions_thread_id_dm_threads_id_fk`) - max jedna sesja na wątek |
| `claude_session_id` | uuid | NOT NULL | - | ID sesji Claude Code CLI |
| `status` | `draft_status` (enum) | NOT NULL | `'idle'` | |
| `current_draft` | text | NULL | - | |
| `iterations_count` | integer | NOT NULL | `0` | |
| `last_error` | text | NULL | - | |
| `created_at` | timestamptz | NOT NULL | `now()` | |
| `updated_at` | timestamptz | NOT NULL | `now()` | trigger `set_draft_sessions_updated_at` |

Indeks: `idx_draft_sessions_status` btree(`status`).

### 4.9 `draft_iterations` - historia iteracji draftu

| Kolumna | Typ | NULL | Default | Uwagi |
|---|---|---|---|---|
| `id` | bigserial | NOT NULL | auto | PK |
| `draft_session_id` | bigint | NOT NULL | - | FK -> `draft_sessions.id` ON DELETE **cascade** (`draft_iterations_draft_session_id_draft_sessions_id_fk`) |
| `iteration_kind` | `iteration_kind` (enum) | NOT NULL | - | |
| `user_instruction` | text | NULL | - | |
| `draft_text` | text | NOT NULL | - | |
| `tokens_used` | integer | NULL | - | |
| `cost_usd` | numeric(10,6) | NULL | - | |
| `created_at` | timestamptz | NOT NULL | `now()` | |

Brak indeksów poza PK. Brak `updated_at`.

### 4.10 `community_members` - cache członków społeczności

| Kolumna | Typ | NULL | Default | Uwagi |
|---|---|---|---|---|
| `id` | bigserial | NOT NULL | auto | PK |
| `admin_account_id` | bigint | NOT NULL | - | FK -> `admin_accounts.id` ON DELETE **cascade** (`community_members_admin_account_id_admin_accounts_id_fk`) |
| `circle_community_member_id` | bigint | NOT NULL | - | |
| `name` | text | NOT NULL | - | |
| `email` | text | NULL | - | |
| `avatar_url` | text | NULL | - | |
| `headline` | text | NULL | - | |
| `bio` | text | NULL | - | |
| `location` | text | NULL | - | |
| `last_seen_text` | text | NULL | - | |
| `status` | text | NULL | - | zwykły text, NIE enum |
| `is_admin` | boolean | NOT NULL | `false` | |
| `can_send_message` | boolean | NOT NULL | `true` | |
| `raw_payload` | jsonb | NULL | - | |
| `fetched_at` | timestamptz | NOT NULL | `now()` | |

Constraint UNIQUE: `uniq_account_member` UNIQUE(`admin_account_id`, `circle_community_member_id`).
Indeks: `idx_members_name` btree(`admin_account_id`, `name`).

### 4.11 `sent_messages` - audyt wysłanych wiadomości

| Kolumna | Typ | NULL | Default | Uwagi |
|---|---|---|---|---|
| `id` | bigserial | NOT NULL | auto | PK |
| `thread_id` | bigint | NOT NULL | - | FK -> `dm_threads.id` ON DELETE **cascade** (od migracji 0004; constraint `sent_messages_thread_id_dm_threads_id_fk`) |
| `body` | text | NOT NULL | - | |
| `circle_message_id` | bigint | NULL | - | |
| `circle_creation_uuid` | uuid | NULL | - | dodane w 0001 |
| `sent_at` | timestamptz | NOT NULL | `now()` | |
| `draft_session_id` | bigint | NULL | - | FK -> `draft_sessions.id` ON DELETE **set null** (od 0004; `sent_messages_draft_session_id_draft_sessions_id_fk`) - audyt zachowuje body/circle id po skasowaniu sesji draftu |
| `error` | text | NULL | - | |

Brak indeksów poza PK.

### 4.12 `app_settings` - singleton ustawień globalnych

| Kolumna | Typ | NULL | Default | Uwagi |
|---|---|---|---|---|
| `id` | integer | NOT NULL (PK) | `1` | PK, NIE serial |
| `global_meta_prompt` | text | NOT NULL | `''` | |
| `format_prompt` | text | NOT NULL | `''` | seed w 0007 (sekcja 9.2) |
| `draft_model` | text | NULL | - | |
| `format_model` | text | NULL | - | |
| `no_reply_threshold_days` | integer | NOT NULL | `3` | "Brak odpowiedzi": ja wysłałem ostatnią, oni milczą od X dni |
| `silence_threshold_days` | integer | NOT NULL | `14` | "Cisza": żadna strona nie pisała od X dni |
| `updated_at` | timestamptz | NOT NULL | `now()` | **brak triggera** - aktualizacja w kodzie |

Constraint CHECK: `app_settings_singleton` CHECK (`id` = 1). **UWAGA: tego CHECK-a nie ma w `schema.ts`** (Drizzle go nie modeluje), istnieje tylko w ręcznej migracji 0003. W porcie musi trafić do DDL/Alembica.

Seed (0003): `INSERT INTO "app_settings" ("id") VALUES (1) ON CONFLICT DO NOTHING;` - wiersz singletona istnieje zawsze po migracjach.

### 4.13 `kb_documents` - baza wiedzy (dokumenty wstrzykiwane do promptów)

`scope='global'` dotyczy każdego konta; `scope='account'` przypięte do jednego `admin_account` (cascade przy jego usunięciu). `body_text` idzie do modelu; `original_*` trzyma upload (base64) do re-download/re-ekstrakcji. `token_estimate` zasila licznik pojemności w UI.

| Kolumna | Typ | NULL | Default | Uwagi |
|---|---|---|---|---|
| `id` | bigserial | NOT NULL | auto | PK |
| `scope` | `kb_scope` (enum) | NOT NULL | - | |
| `admin_account_id` | bigint | NULL | - | FK -> `admin_accounts.id` ON DELETE **cascade** (`kb_documents_admin_account_id_admin_accounts_id_fk`); NULL dla scope='global' |
| `title` | text | NOT NULL | - | |
| `body_text` | text | NOT NULL | `''` | |
| `source_kind` | `kb_source_kind` (enum) | NOT NULL | - | |
| `original_filename` | text | NULL | - | |
| `original_mime` | text | NULL | - | |
| `original_data_b64` | text | NULL | - | plik jako base64 w kolumnie text |
| `token_estimate` | integer | NOT NULL | `0` | |
| `enabled` | boolean | NOT NULL | `true` | |
| `created_at` | timestamptz | NOT NULL | `now()` | |
| `updated_at` | timestamptz | NOT NULL | `now()` | trigger `set_kb_documents_updated_at` |

Indeksy: `idx_kb_scope` btree(`scope`); `idx_kb_account` btree(`admin_account_id`).
Brak constraintu wymuszającego spójność scope vs admin_account_id (tylko logika aplikacyjna).

### 4.14 `assistant_conversations` - rozmowy panelu AI Assistant

| Kolumna | Typ | NULL | Default | Uwagi |
|---|---|---|---|---|
| `id` | bigserial | NOT NULL | auto | PK |
| `auth_account_id` | bigint | NOT NULL | - | FK -> `auth_accounts.id` ON DELETE **cascade** (`assistant_conversations_auth_account_id_auth_accounts_id_fk`) |
| `title` | text | NULL | - | |
| `last_message_at` | timestamptz | NULL | - | |
| `created_at` | timestamptz | NOT NULL | `now()` | |
| `updated_at` | timestamptz | NOT NULL | `now()` | trigger `set_assistant_conversations_updated_at` |

Indeks: `idx_asst_conv_auth` btree(`auth_account_id`, `last_message_at`).

### 4.15 `assistant_messages` - wiadomości asystenta

`raw_content` = surowy output modelu (audyt), `content` = oczyszczony tekst do UI. Propozycja edycji in-app ląduje w `action_proposal` (JSON); `applied_at` ustawiane gdy user kliknie Zastosuj.

| Kolumna | Typ | NULL | Default | Uwagi |
|---|---|---|---|---|
| `id` | bigserial | NOT NULL | auto | PK |
| `conversation_id` | bigint | NOT NULL | - | FK -> `assistant_conversations.id` ON DELETE **cascade** (`assistant_messages_conversation_id_assistant_conversations_id_fk`) |
| `role` | `assistant_msg_role` (enum) | NOT NULL | - | |
| `content` | text | NOT NULL | - | |
| `raw_content` | text | NULL | - | |
| `context_snapshot` | jsonb | NULL | - | |
| `action_proposal` | jsonb | NULL | - | |
| `applied_at` | timestamptz | NULL | - | |
| `apply_error` | text | NULL | - | |
| `tokens_used` | integer | NULL | - | |
| `cost_usd` | numeric(10,6) | NULL | - | |
| `created_at` | timestamptz | NOT NULL | `now()` | |
| `updated_at` | timestamptz | NOT NULL | `now()` | trigger `set_assistant_messages_updated_at` |

Indeks: `idx_asst_msg_conv` btree(`conversation_id`, `created_at`).

### 4.16 `feedback_items` - feedback / pomysły cross-tool

`scope` ustawiany automatycznie ze strony, na której powstał wpis (`'circle-dm'` / `'general'` / przyszłe toole). UI: admin przeglądający (heurystyka: email zawiera 'tomasz') widzi badge z liczbą open.

| Kolumna | Typ | NULL | Default | Uwagi |
|---|---|---|---|---|
| `id` | bigserial | NOT NULL | auto | PK |
| `auth_account_id` | bigint | NOT NULL | - | FK -> `auth_accounts.id` ON DELETE **cascade** (`feedback_items_auth_account_id_auth_accounts_id_fk`) |
| `scope` | text | NOT NULL | `'general'` | zwykły text, NIE enum |
| `body` | text | NOT NULL | - | |
| `status` | `feedback_status` (enum) | NOT NULL | `'open'` | |
| `done_at` | timestamptz | NULL | - | |
| `created_at` | timestamptz | NOT NULL | `now()` | |
| `updated_at` | timestamptz | NOT NULL | `now()` | trigger `set_feedback_items_updated_at` |

Indeks: `idx_feedback_status` btree(`status`, `created_at`).

## 5. Zbiorczo: foreign keys i akcje ON DELETE

| Tabela.kolumna | Cel | ON DELETE | ON UPDATE |
|---|---|---|---|
| `auth_sessions.auth_account_id` | `auth_accounts.id` | cascade | no action |
| `dm_threads.admin_account_id` | `admin_accounts.id` | cascade | no action |
| `thread_checkups.thread_id` | `dm_threads.id` | cascade | no action |
| `dm_messages.thread_id` | `dm_threads.id` | cascade | no action |
| `message_image_descriptions.message_id` | `dm_messages.id` | cascade | no action |
| `draft_sessions.thread_id` | `dm_threads.id` | cascade | no action |
| `draft_iterations.draft_session_id` | `draft_sessions.id` | cascade | no action |
| `community_members.admin_account_id` | `admin_accounts.id` | cascade | no action |
| `sent_messages.thread_id` | `dm_threads.id` | cascade | no action |
| `sent_messages.draft_session_id` | `draft_sessions.id` | **set null** | no action |
| `kb_documents.admin_account_id` | `admin_accounts.id` | cascade | no action |
| `assistant_conversations.auth_account_id` | `auth_accounts.id` | cascade | no action |
| `assistant_messages.conversation_id` | `assistant_conversations.id` | cascade | no action |
| `feedback_items.auth_account_id` | `auth_accounts.id` | cascade | no action |

Efekt łańcucha: usunięcie `admin_accounts` kasuje wątki -> wiadomości -> opisy obrazków, checkupy, draft sessions -> iteracje, sent_messages (cascade), members, kb account-scoped. Usunięcie `auth_accounts` kasuje sesje, rozmowy asystenta -> wiadomości asystenta, feedback.

## 6. Zbiorczo: constrainty UNIQUE (dokładne nazwy - kod robi po nich ON CONFLICT)

| Nazwa | Tabela | Kolumny |
|---|---|---|
| `auth_accounts_email_unique` | auth_accounts | (email) |
| `uniq_account_room` | dm_threads | (admin_account_id, circle_chat_room_uuid) |
| `uniq_thread_message` | dm_messages | (thread_id, circle_message_id) |
| `uniq_msg_image_idx` | message_image_descriptions | (message_id, attachment_index) |
| `draft_sessions_thread_id_unique` | draft_sessions | (thread_id) |
| `uniq_account_member` | community_members | (admin_account_id, circle_community_member_id) |

Plus CHECK: `app_settings_singleton` CHECK (id = 1) na `app_settings`.

## 7. Mapowanie nazwa-w-TS -> kolumna PG

Reguła: camelCase -> snake_case, każda kolumna nazwana jawnie w schema.ts. Pełna lista (pomijam identyczne jak `id`, `email`, `label`, `body`, `title`, `note`, `scope`, `status`, `error`, `content`, `role`, `enabled`, `bio`, `name`, `headline`, `location`, `description`, `attempts`):

| TS (Drizzle property) | Kolumna PG |
|---|---|
| passwordHash | password_hash |
| createdAt | created_at |
| updatedAt | updated_at |
| authAccountId | auth_account_id |
| expiresAt | expires_at |
| lastSeenAt | last_seen_at |
| ipAddr | ip_addr |
| userAgent | user_agent |
| circleAdminToken | circle_admin_token |
| circleRefreshToken | circle_refresh_token |
| circleAccessToken | circle_access_token |
| circleAccessTokenExpiresAt | circle_access_token_expires_at |
| communityId | community_id |
| communityMemberId | community_member_id |
| systemPrompt | system_prompt |
| isActive | is_active |
| lastSyncedAt | last_synced_at |
| adminAccountId | admin_account_id |
| circleChatRoomId | circle_chat_room_id |
| circleChatRoomUuid | circle_chat_room_uuid |
| chatRoomKind | chat_room_kind |
| chatRoomName | chat_room_name |
| otherParticipantEmail | other_participant_email |
| otherParticipantName | other_participant_name |
| otherParticipantId | other_participant_id |
| otherParticipantAvatarUrl | other_participant_avatar_url |
| unreadMessagesCount | unread_messages_count |
| pinnedAt | pinned_at |
| isFlagged | is_flagged |
| lastMessageAt | last_message_at |
| lastMessageSenderId | last_message_sender_id |
| lastMessageSenderIsMe | last_message_sender_is_me |
| lastMessagePreview | last_message_preview |
| rawPayload | raw_payload |
| messagesFetchedAt | messages_fetched_at |
| fetchedAt | fetched_at |
| threadId | thread_id |
| dueAt | due_at |
| doneAt | done_at |
| circleMessageId | circle_message_id |
| richTextBody | rich_text_body |
| senderId | sender_id |
| senderName | sender_name |
| senderIsMe | sender_is_me |
| parentMessageId | parent_message_id |
| chatThreadId | chat_thread_id |
| editedAt | edited_at |
| voiceTranscript | voice_transcript |
| voiceTranscriptStatus | voice_transcript_status |
| voiceTranscriptError | voice_transcript_error |
| voiceTranscriptAttempts | voice_transcript_attempts |
| voiceDurationSec | voice_duration_sec |
| voiceTranscribedAt | voice_transcribed_at |
| messageId | message_id |
| attachmentIndex | attachment_index |
| attachmentUrl | attachment_url |
| describedAt | described_at |
| claudeSessionId | claude_session_id |
| currentDraft | current_draft |
| iterationsCount | iterations_count |
| lastError | last_error |
| draftSessionId | draft_session_id |
| iterationKind | iteration_kind |
| userInstruction | user_instruction |
| draftText | draft_text |
| tokensUsed | tokens_used |
| costUsd | cost_usd |
| circleCommunityMemberId | circle_community_member_id |
| avatarUrl | avatar_url |
| lastSeenText | last_seen_text |
| isAdmin | is_admin |
| canSendMessage | can_send_message |
| circleCreationUuid | circle_creation_uuid |
| sentAt | sent_at |
| globalMetaPrompt | global_meta_prompt |
| formatPrompt | format_prompt |
| draftModel | draft_model |
| formatModel | format_model |
| noReplyThresholdDays | no_reply_threshold_days |
| silenceThresholdDays | silence_threshold_days |
| bodyText | body_text |
| sourceKind | source_kind |
| originalFilename | original_filename |
| originalMime | original_mime |
| originalDataB64 | original_data_b64 |
| tokenEstimate | token_estimate |
| conversationId | conversation_id |
| rawContent | raw_content |
| contextSnapshot | context_snapshot |
| actionProposal | action_proposal |
| appliedAt | applied_at |
| applyError | apply_error |

REST API panelu zwraca pola camelCase (frontend React zostaje), więc port FastAPI musi serializować snake_case DB -> camelCase JSON.

## 8. Funkcja i triggery SQL (dosłownie, instalowane w migrate.ts po migracjach)

```sql
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ language 'plpgsql';
```

```sql
DROP TRIGGER IF EXISTS set_admin_accounts_updated_at ON admin_accounts;
CREATE TRIGGER set_admin_accounts_updated_at BEFORE UPDATE ON admin_accounts
  FOR EACH ROW EXECUTE PROCEDURE update_updated_at_column();

DROP TRIGGER IF EXISTS set_draft_sessions_updated_at ON draft_sessions;
CREATE TRIGGER set_draft_sessions_updated_at BEFORE UPDATE ON draft_sessions
  FOR EACH ROW EXECUTE PROCEDURE update_updated_at_column();

DROP TRIGGER IF EXISTS set_kb_documents_updated_at ON kb_documents;
CREATE TRIGGER set_kb_documents_updated_at BEFORE UPDATE ON kb_documents
  FOR EACH ROW EXECUTE PROCEDURE update_updated_at_column();

DROP TRIGGER IF EXISTS set_assistant_conversations_updated_at ON assistant_conversations;
CREATE TRIGGER set_assistant_conversations_updated_at BEFORE UPDATE ON assistant_conversations
  FOR EACH ROW EXECUTE PROCEDURE update_updated_at_column();
DROP TRIGGER IF EXISTS set_assistant_messages_updated_at ON assistant_messages;
CREATE TRIGGER set_assistant_messages_updated_at BEFORE UPDATE ON assistant_messages
  FOR EACH ROW EXECUTE PROCEDURE update_updated_at_column();

DROP TRIGGER IF EXISTS set_feedback_items_updated_at ON feedback_items;
CREATE TRIGGER set_feedback_items_updated_at BEFORE UPDATE ON feedback_items
  FOR EACH ROW EXECUTE PROCEDURE update_updated_at_column();
```

Tabele z kolumną `updated_at` ale **BEZ triggera** (aktualizuje kod aplikacji): `auth_accounts`, `app_settings`.

## 9. Seedy danych z migracji

### 9.1 Migracja 0003 - wiersz singletona app_settings

```sql
INSERT INTO "app_settings" ("id") VALUES (1) ON CONFLICT DO NOTHING;
```

### 9.2 Migracja 0007 - domyślny format_prompt (DOSŁOWNIE; warunek: tylko gdy nadal pusty, żeby celowo wyczyszczony pozostał pusty; ma odzwierciedlać DEFAULT_FORMAT_PROMPT w `services/format-orchestrator.ts` - trzymać w sync)

```sql
UPDATE "app_settings"
SET "format_prompt" = $$Twoje zadanie: wziąć tekst od użytkownika i przerobić go w finalną wiadomość DM do drugiej osoby, zgodnie z personą i kontekstem rozmowy.

Tekst od użytkownika może być:
- Roboczym draftem (gotowym do polerowania)
- Brain dumpem z dyktowania (luźne notatki, ad-hoc gramatyka)
- Krótką instrukcją tego co chcę napisać (np. "zaproś go na spotkanie we wtorek")

Wytyczne:
- Zachowaj naturalny, mówiony ton z persony — to nie ma być korpomowa.
- Popraw gramatykę i interpunkcję, ale **nie wygładzaj do bezpłciowego stylu**.
- Jeśli to brain dump — zrekonstruuj wiadomość w pierwszej osobie zgodnie z personą.
- Jeśli to gotowy draft — popraw co trzeba, ale zachowaj sens.
- Krótko (zwykle 1–4 zdania).
- Zwróć WYŁĄCZNIE finalną treść wiadomości — bez prefiksu "Oto:", bez wyjaśnień, bez cudzysłowów.$$
WHERE id = 1 AND format_prompt = '';
```

(Tekst promptu skopiowany 1:1 włącznie z em-dashami "—" i "1–4"; to treść promptu dla modelu, nie copy UI, nie poprawiać.)

## 10. Historia migracji (kolejność z meta/_journal.json)

| Tag | Co robi |
|---|---|
| 0000_ambitious_silver_surfer | enumy chat_room_kind/draft_status/iteration_kind; tabele admin_accounts (z późniejszymi dropniętymi: polish_system_prompt, draft_model, polish_model), dm_messages, dm_threads, draft_iterations, draft_sessions, sent_messages (thread_id/draft_session_id ON DELETE no action); FK + indeksy bazowe |
| 0001_left_amphibian | sent_messages + circle_creation_uuid uuid |
| 0002_concerned_gravity | community_members + FK + idx_members_name |
| 0003_app_settings (ręczna) | app_settings (id=1 default, CHECK app_settings_singleton) + seed wiersza |
| 0004_legal_iron_lad | przepięcie FK sent_messages: thread_id -> cascade, draft_session_id -> set null |
| 0005_steep_elektra | app_settings + format_prompt; admin_accounts - polish_system_prompt |
| 0006_sturdy_hardball | app_settings + draft_model, format_model; admin_accounts - draft_model, polish_model |
| 0007_seed_format_prompt (ręczna) | seed domyślnego format_prompt |
| 0008_tense_wraith | auth_accounts + auth_sessions |
| 0009_curly_zarek | enum thread_status; thread_checkups; app_settings + no_reply/silence_threshold_days; dm_threads + status, is_flagged + 2 indeksy |
| 0010_magenta_vision | enumy kb_scope, kb_source_kind; kb_documents |
| 0011_even_banshee | enum assistant_msg_role; assistant_conversations + assistant_messages |
| 0012_flawless_yellow_claw | enum feedback_status; feedback_items |
| 0013_gigantic_joystick | enum voice_transcript_status; dm_messages + 6 kolumn voice_* + indeks |
| 0014_careless_vulture | enum image_description_status; message_image_descriptions |

## Uwagi dla portu na FastAPI

1. **CHECK `app_settings_singleton` istnieje tylko w prod-DB i migracji 0003, nie w schema.ts.** Jeśli generujesz modele SQLAlchemy ze schema.ts, łatwo go zgubić. Wiersz `id=1` musi istnieć zawsze (seed), a kod robi UPDATE ... WHERE id=1, nie INSERT.
2. **Triggery `updated_at` nie są w plikach migracji**, instaluje je migrate.ts po `migrate()`. W Alembicu daj je jako osobną migrację (albo idempotentny krok startowy). I odwrotnie: `auth_accounts.updated_at` oraz `app_settings.updated_at` NIE mają triggera - tu updated_at ustawia kod aplikacji; nie dodawaj triggera "dla porządku", bo zmienisz zachowanie (np. update last-login-like pól zacznie ruszać updated_at).
3. **Prod DB już istnieje.** Drizzle trzyma stan w `drizzle.__drizzle_migrations`. Przy przejściu na Alembic: nie odpalaj DDL od zera na prodzie, zrób baseline (`alembic stamp`) na obecnym stanie i zostaw tabelę drizzle nietkniętą (albo usuń dopiero po pełnym przejściu).
4. **Nazwy constraintów UNIQUE są kontraktem**: kod aplikacji robi upserty `ON CONFLICT` na `uniq_account_room`, `uniq_thread_message`, `uniq_msg_image_idx`, `uniq_account_member`, `draft_sessions_thread_id_unique`. W SQLAlchemy nadaj identyczne nazwy (świeża baza z autonaming convention je zmieni i upserty po nazwie/kolumnach muszą się zgadzać).
5. **`numeric(10,6)` (cost_usd)**: drizzle/postgres-js zwraca numeric jako **string** w JS, więc API mogło serializować costUsd jako string. W Pythonie Decimal -> sprawdź w spec API jak frontend tego oczekuje i zachowaj format.
6. **bigint jako number**: wszystkie bigint mają w Drizzle `mode: 'number'` - JSON niesie liczby, nie stringi. W Pydantic zwykły `int` (ID z Circle mieszczą się w double, ale uważaj gdyby kiedyś przekroczyły 2^53).
7. **timestamptz wszędzie** (`withTimezone: true`). W SQLAlchemy `DateTime(timezone=True)`, w Pydantic serializacja ISO 8601 z offsetem - frontend dostawał ISO stringi z postgres-js/Drizzle.
8. **`dm_messages.created_at` i `sender_is_me` nie mają defaultów** - sync z Circle musi je zawsze podać. `auth_sessions.id` to text PK (32-bajtowy hex = 64 znaki, wartość cookie), nie UUID i nie serial.
9. **Kolumny uuid**: `dm_threads.circle_chat_room_uuid`, `draft_sessions.claude_session_id`, `sent_messages.circle_creation_uuid` to natywny typ PG `uuid` - waliduj format przed insertem, PG odrzuci śmieci.
10. **`idx_checkups_pending_due` to NIE indeks częściowy** mimo nazwy - zwykły btree na `due_at`. Odtwórz tak samo, nie "ulepszaj" na partial.
11. **Drift schema vs migracje**: admin_accounts w 0000 miał `polish_system_prompt`, `draft_model`, `polish_model` - dropnięte w 0005/0006 (modelka przeniesiona globalnie do app_settings). Jeśli budujesz świeże DDL ze stanu końcowego, te kolumny mają nie istnieć.
12. **Pool**: postgres-js max=10, idle_timeout=30 s, prepared statements ON, graceful close z timeoutem 5 s. W asyncpg/SQLAlchemy dobierz analogicznie (max_size=10, ewentualnie pool_recycle); to soft-parametry, nie kontrakt, ale nie schodź z max bez powodu (sync Circle + WS robią równoległe zapytania).
13. **`kb_documents.original_data_b64`**: pliki (PDF) siedzą jako base64 w kolumnie text - potencjalnie duże wiersze. Przy SELECT-ach listujących dokumenty oryginalny kod raczej nie ciągnie tej kolumny; w porcie też wyłącz ją z listingów (deferred column), inaczej zabijesz wydajność.
14. **`feedback_items.scope` i `community_members.status` to text, nie enumy** - nie zamieniaj na Enum w Pydantic z zamkniętą listą wartości.
