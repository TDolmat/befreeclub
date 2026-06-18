# Kontrakt portu (FastAPI) - konwencje, własność plików, sygnatury cross-module

Ten plik to umowa między agentami piszącymi port. Fundament (core, modele, migracja,
main, stuby) już istnieje - NIE przepisuj go. Każdy agent pisze TYLKO swoje pliki
z sekcji 2 (stuby nadpisuje w całości). Specyfikacje w `docs/spec/*.md` są kontraktem
zachowania, oryginalny TS w `/Users/tomasz/repos/befreeclub/admin` jest źródłem prawdy.

## 1. Konwencje (obowiązują wszystkich)

### 1.1 Błędy HTTP

- Globalne handlery są już w `app/main.py`. NIE zwracaj FastAPI-owego `{"detail": ...}`.
- Nieobsłużony wyjątek → `500 {"error": "<str(exc)>"}` (handler globalny, nie łap sam).
- Błąd "kontrolowany" w route: `raise HTTPException(status_code=..., detail="<komunikat>")`
  → handler zamienia na `{"error": "<komunikat>"}` z tym statusem. Przykład:
  `HTTPException(401, "Unauthorized")` → `401 {"error":"Unauthorized"}`.
  Alternatywnie zwróć wprost `JSONResponse({"error": ...}, status_code=...)` - oba OK.
- Walidacja requestu (Pydantic w body/query/path) → **400** `{"error": "Invalid request"}`
  (handler `RequestValidationError` już jest; oryginał zwracał 400 z dumpem zoda,
  kształt body nie jest load-bearing, status 400 TAK). Nienumeryczne `:id` w ścieżce
  = 400, NIE 404 → w route'ach typuj parametry path jako `int` i pozwól walidacji upaść.
- Tam gdzie oryginał zwraca błąd inline (np. `{"error":"Thread not found"}` z 404),
  kopiuj string co do bajtu ze speca/TS.

### 1.2 JSON API

- camelCase 1:1 ze starym API. DB ma NOWE nazwy (`account_id`, `user_id`), JSON STARE
  (`adminAccountId`, `authAccountId`) - mapowanie robi DTO przez jawny alias
  (`Field(alias="adminAccountId")` / `serialization_alias`), bo auto-`to_camel`
  z `account_id` dałoby `accountId`.
- Baza DTO: `app/core/schemas.py` → `CamelModel` (alias_generator=to_camel,
  populate_by_name, from_attributes), `IsoDateTime`, `NumericStr`,
  `dump(model) -> dict` (= `model_dump(mode="json", by_alias=True)`).
- `datetime` w JSON: ZAWSZE typ `IsoDateTime` → `YYYY-MM-DDTHH:MM:SS.sssZ`
  (jak `Date.toISOString()` w Node). Ręcznie: `app.core.logging.to_iso_string(dt)`.
- `numeric` z PG (`cost_usd`): asyncpg zwraca `Decimal`; w JSON ma być **string**
  (tak serializował postgres-js) → typ `NumericStr`. Wyjątek: WS `draft:complete`
  niesie `costUsd` jako number|null (wartość z parsera CLI, float) - tam zostaje liczba.
- Pole nullable (TS `x: T | null`) = obecne w JSON jako `null`. Pole opcjonalne
  (TS `x?: T`) = POMIJANE gdy brak. NIE używaj `exclude_none` globalnie - buduj
  dict ręcznie albo `dump(model, exclude_none=True)` tylko dla modeli, w których
  KAŻDE None znaczy "pomiń" (np. `MeResponse`).
- bigint = zwykły `int` w JSON (Drizzle mode:'number').

### 1.3 DB

- Modele: `app/modules/admin/models.py` (User, Session, FeedbackItem),
  `app/modules/circle_dm/models.py` (Account, Thread, Checkup, Message,
  MessageImageDescription, DraftSession, DraftIteration, Member, SentMessage,
  AppSettings, KbDocument, AssistantConversation, AssistantMessage). Gotowe, nie ruszać.
- Sesje DB: route'y przez dependency `Depends(get_session)` z `app.core.db`;
  serwisy wołane spoza route'ów (workery, orchestratory) otwierają własne:
  `async with async_session_maker() as session: ...` (port globalnego `db` z TS).
  Po zapisie JAWNY `await session.commit()`.
- Upserty `ON CONFLICT`: `sqlalchemy.dialects.postgresql.insert` +
  `on_conflict_do_update(index_elements=[...])` (kolumny, nie nazwy constraintów).
  Pary kolumn jak w db-schema.md sekcja 6.
- Odróżnienie INSERT/UPDATE przy upsercie wiadomości: `.returning(..., text("(xmax = 0)"))`.
- `kb_documents.original_data_b64` NIE może być selectowane w listingach.
- `updated_at`: triggery DB tylko dla accounts, draft_sessions, kb_documents,
  assistant_conversations, assistant_messages, feedback_items. Dla `admin.users`
  i `circle_dm.settings` ustawia KOD (jak oryginał). Nie dodawaj triggerów.

### 1.4 Auth

- Dependency: `from app.modules.admin.services.auth import require_auth, AuthContext, DEV_FAKE_AUTH`.
  `require_auth(request) -> AuthContext` (pola: `auth_account_id: int`, `email: str`).
  Dev (`settings.NODE_ENV != "production"`): zwraca `DEV_FAKE_AUTH` (0, dev@local).
  Prod: cookie `admin_session` → `validate_session` (sliding window!); brak/zła →
  `HTTPException(401, "Unauthorized")`.
- Montowanie już zrobione w `app/main.py`: `/api/auth` publiczne, feedback +
  circle-dm za `Depends(require_auth)` na routerze. Route, który potrzebuje
  tożsamości (feedback POST, assistant), bierze `auth: AuthContext = Depends(require_auth)`.
- scrypt: `app.core.security` → `hash_password`, `verify_password`, `DUMMY_HASH`,
  oraz `hash_password_async` / `verify_password_async` (threadpool - W ROUTE'ACH
  UŻYWAJ WERSJI ASYNC, scrypt blokuje ~150 ms).

### 1.5 WS / współbieżność / CLI

- Broker: `from app.core.ws import broker` (singleton). `broker.broadcast(event: dict)`
  jest SYNCHRONICZNY - wołaj bez await, też z callbacków parsera CLI. Eventy:
  dicty z literalnymi kluczami camelCase wg core-infra.md sekcja 3, np.
  `broker.broadcast({"type": "draft:token", "threadId": tid, "chunk": c, "iterationKind": "initial"})`.
  Pola `error?` dodawaj do dicta TYLKO gdy są (pomijane ≠ null).
- Semafor: `from app.core.semaphore import Semaphore`. KAŻDY orchestrator
  (draft, compose, format, assistant) tworzy WŁASNĄ instancję module-level
  `_sem = Semaphore(settings.CLAUDE_MAX_CONCURRENT)`. NIE współdziel (quirk 1:1).
  Wzorzec: `release = await _sem.acquire(); try: ... finally: release()`.
- Claude CLI: `from app.core.claude_cli import run_claude, ClaudeStreamHandlers, RunClaudeResult`.

  ```python
  async def run_claude(
      prompt: str, *,
      session_id: str | None = None,          # --session-id (nowa sesja)
      resume_session_id: str | None = None,   # --resume (priorytet nad session_id)
      append_system_prompt: str | None = None,
      model: str | None = None,
      on_spawn: Callable[[asyncio.subprocess.Process], None] | None = None,
      handlers: ClaudeStreamHandlers | None = None,
  ) -> RunClaudeResult
  # RunClaudeResult: exit_code (int | None - None = zabity sygnałem, ścieżka
  # cancel jak null w Node), stderr (cały), text (zaakumulowane bloki),
  # session_id, tokens_used (input+output albo None), cost_usd (float | None)
  ```

  Decyzje o `exit_code != 0` i pustym `text` podejmuje CALLER z własnymi
  komunikatami (draft/compose/format: `claude exited with code {code}: {stderr[:500]}`,
  asystent: `claude exited {code}: {stderr[:300]}`; pusty: `claude returned empty draft`
  / `claude returned empty result` / `empty response`). BEZ timeoutu. Cancel =
  `proc.terminate()` (uchwyt z `on_spawn`). Rotacja sesji: świeży
  `str(uuid.uuid4())` przy KAŻDYM generowaniu, `resume` nieużywany.
- Logger: `from app.core.logging import create_logger; log = create_logger("scope")`.
  Scope'y jak w TS: server, ws, claude:spawn, polling, sync, send, draft, compose,
  format, assistant, kb, members, circle:client, circle:jwt, stt, vision,
  voice-worker, image-worker, auth, feedback, health.
- Workery: wzorzec async task w module - idempotentny `start_*()` (no-op gdy task żyje),
  natychmiastowy pierwszy tick, potem `asyncio.sleep(interval)`; flaga reentrancy
  (tick pomijany, nie kolejkowany); `stop_*()` = cancel taska. Fire-and-forget:
  `asyncio.create_task` + trzymaj referencję + odłów wyjątku do log.warn.

### 1.6 Config

`from app.core.config import settings` - nazwy pól = nazwy env (NODE_ENV, PORT,
DATABASE_URL, DB_HOST/DB_PORT/DB_USER/DB_PASS/DB_NAME, CLAUDE_BIN_PATH, DRAFT_MODEL,
POLISH_MODEL, CLAUDE_MAX_CONCURRENT, POLLING_INTERVAL_MS, KB_BUDGET_TOKENS,
KB_HARD_CEILING_TOKENS, LOG_LEVEL, BOOTSTRAP_ADMIN_*, WEB_DIST_PATH, OPENAI_API_KEY,
OPENAI_WHISPER_MODEL, OPENAI_VISION_MODEL, VOICE_TRANSCRIPT_INTERVAL_MS,
IMAGE_DESCRIPTION_INTERVAL_MS). Puste stringi opcjonalnych = None.
`KB_BUDGET_TOKENS`/`KB_HARD_CEILING_TOKENS` czytaj z settings (w TS były stałymi).

### 1.7 Zachowane quirki oryginału (NIE naprawiać)

Pełna, finalna lista różnic port vs oryginał (naprawione + zachowane quirki
+ odstępstwa techniczne): `docs/spec/port-odstepstwa.md`.

- `{"ok": true}` przy 0 dopasowań dla PATCH checkup done / DELETE checkup
  / DELETE account (front samoleczy refetchem, nie ma onError).
- Semafory per moduł orchestratora (realny limit 4×CLAUDE_MAX_CONCURRENT).
- WS `thread:new_messages` zawsze `newCount: 1` (hardcoded).
- Brak auth na /ws; brak timeoutu w run_claude; deep-probe cache trzyma też porażki 1h.
- `idx_checkups_pending_due` to zwykły btree (nie partial).
- `can_send_message` nadpisywane na true przy każdym pełnym sync members.
- Bulk-send sekwencyjny; auto-revival "szeroki"; polling top 50 wątków / 100 wiadomości.

Świadomie NAPRAWIONE w porcie (szczegóły w port-odstepstwa.md): GET /settings
bez `cachedAt`; asystent bierze model z app_settings.draft_model (fallback env);
checkup done/delete scoped do wątku z URL; PATCH /accounts/:id oraz PATCH
/threads/:id/status|flag zwracają 404 dla nieistniejącego id; sort=next_checkup
sortowany w SQL przed LIMIT.

## 2. Drzewo plików i własność

```
backend/
  pyproject.toml, alembic.ini, alembic/, .env.example        [fundament - GOTOWE]
  scripts/set_auth_password.py                               [fundament - GOTOWE]
  app/
    main.py                                                  [fundament - GOTOWE]
    core/ (config, db, logging, security, semaphore, ws,
           claude_cli, schemas)                              [fundament - GOTOWE]
    modules/
      admin/
        models.py, schemas.py                                [fundament - GOTOWE]
        services/auth.py        (STUB -> pełny port)         [admin-module]
        services/sessions.py    (STUB -> pełny port)         [admin-module]
        services/rate_limit.py  (nowy)                       [admin-module]
        services/claude_health.py (nowy)                     [admin-module]
        routes/auth.py          (STUB -> port)               [admin-module]
        routes/health.py        (STUB -> port)               [admin-module]
        routes/feedback.py      (STUB -> port)               [routes-a]
      circle_dm/
        models.py, schemas.py, services/app_settings.py      [fundament - GOTOWE]
        circle/client.py, jwt_manager.py, tiptap.py,
          attachments.py, types.py (nowe)                    [circle-client]
        services/openai_stt.py, openai_vision.py (nowe)      [media-services]
        services/voice_transcript_worker.py (STUB -> port)   [media-services]
        services/image_description_worker.py (STUB -> port)  [media-services]
        services/thread_sync.py, members_sync.py, send.py,
          bulk_send.py, thread_state.py (nowe)               [sync-services]
        services/polling_worker.py (STUB -> port)            [sync-services]
        services/draft_orchestrator.py, compose_orchestrator.py,
          format_orchestrator.py, assistant_orchestrator.py,
          assistant_actions.py, history_formatter.py,
          knowledge_base.py (nowe)                           [ai-services]
        routes/accounts.py, threads.py, messages.py,
          members.py, settings.py (STUBY -> port)            [routes-a]
        routes/drafts.py, compose.py, format.py, bulk.py,
          kb.py, assistant.py (STUBY -> port)                [routes-b]
```

Route'y: w każdym pliku module-level `router = APIRouter()`; ścieżki RELATYWNE do
prefiksu (main montuje `/api/circle-dm/<segment>` i `/api/auth`, `/api/feedback`;
health bez prefiksu: `@router.get("/health")`, `@router.get("/health/claude")`).

## 3. Sygnatury cross-module (zamrożone - pisz dokładnie takie)

Wszystkie funkcje `async def`, chyba że zaznaczono inaczej. Nazwy snake_case
portu nazw z TS. "dict" = kształt JSON jak w TS/spec (klucze camelCase tam,
gdzie wynik idzie wprost do odpowiedzi/WS).

### 3.1 admin-module (`app.modules.admin.services.*`)

```python
# sessions.py
SESSION_COOKIE_NAME = "admin_session"
SESSION_TTL_MS = 30 * 24 * 60 * 60 * 1000
@dataclass SessionContext: auth_account_id: int; email: str
async def create_session(user_id: int, *, ip_addr: str | None, user_agent: str | None) -> dict
    # {"id": str, "expires_at": datetime}
async def validate_session(session_id: str) -> SessionContext | None   # sliding window
async def invalidate_session(session_id: str) -> None
async def invalidate_all_for_account(user_id: int) -> None
async def purge_expired_sessions() -> int

# auth.py
@dataclass(frozen=True) AuthContext: auth_account_id: int; email: str
DEV_FAKE_AUTH = AuthContext(0, "dev@local")
async def require_auth(request: Request) -> AuthContext     # FastAPI dependency

# rate_limit.py (in-memory, per proces)
def is_locked(key: str) -> dict           # {"locked": bool, "retry_after_sec": int?}
def record_failure(key: str) -> dict      # {"locked_now": bool, "retry_after_sec": int?}
def record_success(key: str) -> None
def client_ip(request: Request) -> str    # cf-connecting-ip > x-real-ip > x-forwarded-for[0] > "unknown"

# claude_health.py
async def get_claude_health(*, deep: bool = False) -> dict  # ClaudeHealth (camelCase!)
```

### 3.2 circle-client (`app.modules.circle_dm.circle.*`)

```python
# client.py
class CircleApiError(Exception):          # .status: int, .body: str; message wg spec
    def __init__(self, status: int, body: str, message: str | None = None): ...
async def exchange_admin_token_for_jwt(admin_token: str, email: str) -> dict
async def list_threads(jwt: str, *, page: int = 1, per_page: int = 50) -> dict
async def get_thread_messages(jwt: str, chat_room_uuid: str, *, per_page: int = 100) -> dict
def text_to_tiptap(text: str) -> dict     # sync; envelope bajt w bajt wg spec
async def send_message(jwt: str, chat_room_uuid: str, body: str) -> dict
async def mark_chat_room_read(jwt: str, chat_room_uuid: str) -> None
async def send_to_new_recipient(jwt: str, community_member_ids: list[int], body: str) -> dict
async def list_members(jwt: str, *, page: int = 1, per_page: int = 100,
                       query: str | None = None) -> dict

# jwt_manager.py
@dataclass JwtState: access_token: str; expires_at: datetime; community_id: int; community_member_id: int
async def get_jwt_for(account_id: int) -> JwtState   # dedupe inflight per konto
async def invalidate_jwt(account_id: int) -> None

# tiptap.py
def tiptap_to_plain_text(doc: object) -> str          # sync

# attachments.py
@dataclass NormalizedAttachment:
    kind: str                  # 'image' | 'video' | 'audio' | 'file'
    url: str
    thumbnail_url: str | None
    full_url: str | None
    filename: str
    content_type: str
    byte_size: int | None
    width: int | None
    height: int | None
    voice_message: bool
def extract_attachments(rich_text_body: object) -> list[NormalizedAttachment]  # sync
    # attachment_index = indeks w PELNEJ liscie (nie wsrod samych obrazkow)
```

### 3.3 sync-services (`app.modules.circle_dm.services.*`)

```python
# thread_sync.py
async def sync_threads_for_account(account_id: int) -> dict
    # {"changed_thread_ids": list[int], "new_unread_thread_ids": list[int],
    #  "stale_message_thread_ids": list[int]} - 1:1 z returnem TS
async def sync_messages_for_thread(thread_id: int) -> int     # liczba INSERTÓW
async def refetch_threads(thread_ids: list[int]) -> None

# polling_worker.py
def start_polling() -> None               # sync, idempotentny
def stop_polling() -> None
async def sync_now(admin_account_id: int | None = None) -> None

# members_sync.py
async def sync_members_for_account(account_id: int) -> int
async def get_cached_members_count(account_id: int) -> int
async def ensure_members_cached(account_id: int) -> None
async def get_member_by_circle_id(account_id: int, circle_community_member_id: int) -> Member | None

# send.py
async def send_draft(thread_id: int, body: str) -> dict
    # {"ok": bool, "circleMessageId": int | None, "error"?: str}; brak wątku -> raise
    # Exception(f"thread {thread_id} not found")

# bulk_send.py
async def send_to_multiple(items: list[dict], body: str) -> list[dict]   # BulkSendResult[]

# thread_state.py
async def set_thread_status(thread_id: int, status: str) -> bool     # False = brak wątku
async def set_thread_flagged(thread_id: int, is_flagged: bool) -> bool
async def bulk_set_status(thread_ids: list[int], status: str) -> int
async def bulk_set_flagged(thread_ids: list[int], is_flagged: bool) -> int
async def list_checkups(thread_id: int) -> list[dict]        # CheckupRow wg spec
async def create_checkup(thread_id: int, due_at: datetime, note: str | None) -> dict
async def mark_checkup_done(thread_id: int, checkup_id: int) -> None   # scoped do wątku
async def delete_checkup(thread_id: int, checkup_id: int) -> None
async def clear_pending_checkups_on_send(thread_id: int) -> None
async def revive_if_done(thread_id: int) -> bool
async def get_latest_pending_checkup(thread_id: int) -> dict | None
```

### 3.4 ai-services (`app.modules.circle_dm.services.*`)

```python
# app_settings.py [GOTOWE - fundament]
@dataclass AppSettingsSnapshot: global_meta_prompt, format_prompt, draft_model,
    format_model, no_reply_threshold_days, silence_threshold_days, cached_at  # epoch ms
async def get_settings() -> AppSettingsSnapshot
async def get_global_meta_prompt() -> str; get_format_prompt() -> str
async def get_draft_model() -> str | None; get_format_model() -> str | None
async def set_global_meta_prompt(v: str); set_format_prompt(v: str)
async def set_draft_model(v: str | None); set_format_model(v: str | None)
async def set_no_reply_threshold_days(v: int); set_silence_threshold_days(v: int)
def compose_system_prompt(persona: str, meta_prompt: str) -> str           # sync
def compose_format_system_prompt(persona, meta_prompt, format_prompt) -> str

# draft_orchestrator.py
async def generate_initial_draft(thread_id: int) -> None
async def set_draft(thread_id: int, draft: str) -> None
async def reset_draft(thread_id: int) -> None
async def mark_sent(thread_id: int) -> None

# compose_orchestrator.py
async def generate_compose_draft(account_id: int, circle_community_member_id: int) -> dict
    # {"draft": str, "tokensUsed": int | None, "costUsd": float | None}
async def send_compose_draft(account_id: int, circle_community_member_id: int, body: str) -> dict
    # {"ok": True, "threadId": int, "circleChatRoomUuid": str} | {"ok": False, "error": str}

# format_orchestrator.py
DEFAULT_FORMAT_PROMPT: str                # bajt w bajt z TS (= seed migracji 0007)
async def format_for_thread(thread_id: int, user_text: str) -> dict      # FormatResult
async def format_for_compose(account_id: int, circle_community_member_id: int, user_text: str) -> dict
async def format_for_bulk(account_id: int, user_text: str) -> dict
    # FormatResult: {"text": str, "tokensUsed": int | None, "costUsd": float | None}

# assistant_orchestrator.py  (auth_account_id = admin.users.id, w DB kolumna user_id)
async def get_or_create_current_conversation(auth_account_id: int) -> dict
async def start_new_conversation(auth_account_id: int) -> dict
async def delete_conversation(conversation_id: int, auth_account_id: int) -> bool
async def get_conversation_full(conversation_id: int, auth_account_id: int) -> dict | None
async def cancel_turn(conversation_id: int, auth_account_id: int) -> bool
async def list_conversations(auth_account_id: int) -> list[dict]
async def run_assistant_turn(*, conversation_id: int, auth_account_id: int,
                             user_text: str, context: dict) -> dict
async def get_message_for_apply(message_id: int, auth_account_id: int) -> dict | None
async def mark_applied(message_id: int, error: str | None) -> None
async def get_message_by_id(message_id: int, auth_account_id: int) -> dict | None

# assistant_actions.py
async def apply_action(proposal: dict) -> None

# history_formatter.py
async def format_thread_history_for_claude(thread_id: int) -> dict   # {"history": str, ...}

# knowledge_base.py  (limity z settings.KB_BUDGET_TOKENS / KB_HARD_CEILING_TOKENS)
def estimate_tokens(text: str) -> int                                  # sync
async def extract_text_from_upload(filename: str, mime: str, data: bytes) -> dict
def invalidate_kb_cache() -> None                                      # sync
async def build_kb_block(account_id: int) -> str
async def get_kb_capacity(account_id: int | None) -> dict              # KbCapacity
```

### 3.5 media-services (`app.modules.circle_dm.services.*`)

```python
# openai_stt.py
class SttConfigError(Exception); class SttFetchError(Exception)
class SttApiError(Exception)             # .status: int
async def transcribe_audio_from_url(audio_url: str) -> dict   # TranscribeResult wg spec

# openai_vision.py
class VisionConfigError(Exception); class VisionFetchError(Exception)
class VisionApiError(Exception)          # .status: int
async def describe_image_from_url(image_url: str) -> dict     # DescribeResult wg spec

# voice_transcript_worker.py
def start_voice_transcript_worker() -> None
def stop_voice_transcript_worker() -> None
async def retry_transcript(message_id: int) -> None

# image_description_worker.py
def start_image_description_worker() -> None
def stop_image_description_worker() -> None
async def retry_image_description(desc_id: int) -> None
```

## 4. Środowisko dev / weryfikacja

- `cd backend && uv sync`; lint: `uv run ruff check app`; smoke: `uv run python -c "import app.main"`.
- Lokalna baza testowa: `befreeclub_test` (Postgres.app, peer auth, user tomasz);
  migracja: `DB_NAME=befreeclub_test uv run alembic upgrade head`. NIE dotykać `bfc_admin`.
- Serwer dev: `uv run python -m app.main` (uvicorn: 1 worker, ws_ping_interval=None -
  oba parametry obowiązkowe, stan jest in-memory, a oryginalny serwer ws nie pinguje).
- Plik `.env` w `backend/` (z `.env.example`); stare `.env` z `DATABASE_URL` też działają.
