# Spec: Claude CLI jako subprocess (spawn + stream-parser)

Źródła (stan na 2026-06-10):

- `admin/apps/server/src/core/claude/spawn.ts` (funkcja `runClaude`)
- `admin/apps/server/src/core/claude/stream-parser.ts` (klasa `ClaudeStreamParser`)
- kontekst pomocniczy (potrzebny do odtworzenia zachowania 1:1): `core/util/semaphore.ts`, `core/env.ts`, `core/health/claude-health.ts` oraz wywołania `runClaude` w 4 orchestratorach Circle DM (`draft-orchestrator.ts`, `compose-orchestrator.ts`, `format-orchestrator.ts`, `assistant-orchestrator.ts`).

Moduł odpala binarkę `claude` (Claude Code CLI) jako proces potomny w trybie nieinteraktywnym, streamuje JSONL ze stdout i emituje typowane eventy do callbacków. To jedyny mechanizm LLM w adminie dla draftów/asystenta (brak API key Anthropic, działa na subskrypcji Max przez `claude login`).

---

## 1. Konfiguracja (env serwera)

Z `core/env.ts` (walidacja Zod, brak/zła wartość = `process.exit(1)` na starcie):

| Zmienna | Typ / walidacja | Default | Użycie |
|---|---|---|---|
| `CLAUDE_BIN_PATH` | string, min 1 znak, **wymagana** | brak | ścieżka do binarki `claude` |
| `DRAFT_MODEL` | string | `claude-sonnet-4-6` | model do draftów/compose/asystenta |
| `POLISH_MODEL` | string | `claude-opus-4-7` | model do formatowania ("polish") |
| `CLAUDE_MAX_CONCURRENT` | int, coerce, min 1, max 8 | `2` | pojemność semafora (per moduł orchestratora, patrz sekcja 8) |

`DRAFT_MODEL` / `POLISH_MODEL` to tylko fallbacki: orchestratory najpierw czytają model z ustawień w DB (`getDraftModel()` / `getFormatModel()`), a `?? env.X` gdy w DB pusto. Wyjątek: asystent bierze `env.DRAFT_MODEL` bezpośrednio, bez DB.

---

## 2. Kontrakt `runClaude`

```
runClaude(opts: RunClaudeOptions): Promise<RunClaudeResult>
```

Opcje (wszystkie poza `prompt` i `handlers` opcjonalne):

| Pole | Typ | Znaczenie |
|---|---|---|
| `prompt` | string | treść user prompta, idzie **stdinem** |
| `sessionId` | string (UUID) | nowa sesja: `--session-id <id>` |
| `resumeSessionId` | string | kontynuacja sesji: `--resume <id>`; **ma priorytet nad `sessionId`** (if/else-if) |
| `appendSystemPrompt` | string | `--append-system-prompt <tekst>` (doklejka do domyślnego system prompta Claude Code) |
| `model` | string | `--model <id>` |
| `onSpawn` | `(child: ChildProcess) => void` | wywołany raz, zaraz po spawn, daje uchwyt do kill (cancel) |
| `handlers` | `ClaudeStreamEvents` | callbacki parsera (sekcja 5) |

Wynik (Promise resolve'uje się dopiero gdy proces się zamknie):

```ts
interface RunClaudeResult {
  exitCode: number | null;  // kod wyjścia; null gdy proces zabity sygnałem
  stderr: string;           // CAŁY stderr zbuforowany w pamięci
}
```

Promise **reject**uje tylko przy zdarzeniu `error` procesu (np. binarka nie istnieje, ENOENT). Niezerowy exit code NIE jest błędem na tym poziomie, decyzję podejmuje caller.

**Brak jakiegokolwiek timeoutu w `runClaude`.** Proces może działać dowolnie długo; jedyny sposób przerwania to kill przez uchwyt z `onSpawn`.

---

## 3. Argumenty CLI - dokładnie

Stała:

```ts
const DISALLOWED_TOOLS = ['Bash', 'Edit', 'Write', 'WebSearch', 'WebFetch'];
```

Budowa argv, w tej kolejności (dosłownie):

1. Zawsze na start: `--print --verbose --output-format stream-json`
2. Jeśli `resumeSessionId`: `--resume <resumeSessionId>`
   inaczej jeśli `sessionId`: `--session-id <sessionId>`
   (gdy brak obu: żadnej z tych flag)
3. Jeśli `appendSystemPrompt`: `--append-system-prompt <treść>`
4. Jeśli `model`: `--model <model>`
5. Zawsze na końcu:
   - `--permission-mode bypassPermissions`
   - `--disallowedTools Bash,Edit,Write,WebSearch,WebFetch` (jedna wartość, join przecinkiem, bez spacji)
   - `--input-format text`

Pełny przykład (nowa sesja, z personą i modelem):

```
claude --print --verbose --output-format stream-json \
  --session-id 3f6c...uuid \
  --append-system-prompt "<persona...>" \
  --model claude-sonnet-4-6 \
  --permission-mode bypassPermissions \
  --disallowedTools Bash,Edit,Write,WebSearch,WebFetch \
  --input-format text
```

**Prompt NIGDY nie jest argumentem pozycyjnym.** Komentarz z kodu (dosłownie):

> Prompt is piped via stdin (NOT as positional arg) to avoid being consumed by variadic flags like --disallowedTools.

Czyli: `--disallowedTools` w CLI jest variadic i pożarłby positional prompt, dlatego prompt idzie stdinem, a `--input-format text` mówi CLI, żeby czytał go ze stdin jako zwykły tekst.

---

## 4. Spawn procesu

```ts
const child = spawn(env.CLAUDE_BIN_PATH, args, {
  stdio: ['pipe', 'pipe', 'pipe'],
  env: { ...process.env },
});
```

- **Env procesu = pełna kopia env serwera**, nic nie jest dodawane ani usuwane. Krytyczne, bo CLI potrzebuje `HOME` (credentials w `~/.claude/.credentials.json` po `claude login`) i `PATH`.
- Brak `cwd` w opcjach (dziedziczy cwd serwera), brak `shell`, brak `detached`.
- Zaraz po spawn: `opts.onSpawn?.(child)`.
- stdin: rejestrowany pusty handler `child.stdin.on('error', () => {})` (ignoruje EPIPE itp. gdy proces padnie zanim przeczyta prompt; błędy i tak wyjdą przez exit code/stderr). Potem `child.stdin.end(opts.prompt)` - jednorazowy zapis całego prompta + zamknięcie stdin.
- stdout: `setEncoding('utf8')`, każdy chunk idzie do `parser.feed(chunk)`.
- stderr: `setEncoding('utf8')`, akumulowany do stringa `stderr` (całość w pamięci) + log debug pierwszych 200 znaków chunka.
- `child.on('error', err)` → log error + `reject(err)`.
- `child.on('close', code)` → `parser.flush()` (dopija ostatnią linię bez `\n`), log debug, `resolve({ exitCode: code, stderr })`.

Logowanie przed spawnem (debug): ścieżka binarki, `mode: 'resume' | 'new'` (resume gdy `resumeSessionId`), `sessionId: resumeSessionId ?? sessionId`, `model`, `promptPreview: prompt.slice(0, 100)`.

---

## 5. Parser stream-json (`ClaudeStreamParser`)

Wyjście CLI przy `--output-format stream-json` to JSONL: jedna linia = jeden event JSON. Parser jest stanowy (bufor na niedokończone linie).

### Interfejs callbacków

```ts
export interface ClaudeStreamEvents {
  onSystemInit?: (sessionId: string) => void;
  onTextDelta?: (text: string) => void;
  onToolUse?: (name: string) => void;
  onResult?: (result: {
    totalCostUsd: number | null;
    inputTokens: number | null;
    outputTokens: number | null;
    durationMs: number | null;
    raw: unknown;
  }) => void;
  onUnknown?: (event: unknown) => void;
  onParseError?: (line: string, err: Error) => void;
}
```

### Algorytm buforowania

- `feed(chunk)`: dokleja chunk do bufora; dopóki w buforze jest `\n`: wytnij linię do `\n`, `trim()`, usuń z bufora (razem z `\n`); pusta linia po trim = pomiń; inaczej `parseLine(line)`.
- `flush()`: na koniec streamu (event `close` procesu) trim resztki bufora, wyzeruj bufor, jeśli niepuste - parsuj jako ostatnią linię.

### Rozpoznawane eventy (dokładne nazwy pól JSON z CLI, snake_case!)

Kolejność sprawdzania w `parseLine`:

1. **Parse error**: `JSON.parse(line)` rzuca → `onParseError(line, err)`, linia porzucona, parsowanie leci dalej.
2. **`type === 'system' && subtype === 'init'`**: jeśli jest `session_id` (string) → `onSystemInit(session_id)`. Return.
3. **`type === 'assistant'` i istnieje `message.content` (tablica)**: iteracja po blokach content:
   - blok `type === 'text'` i `typeof block.text === 'string'` → `onTextDelta(block.text)`
   - blok `type === 'tool_use'` i `typeof block.name === 'string'` → `onToolUse(block.name)`
   - inne typy bloków: ignorowane. Return.
4. **`type === 'result'`** → `onResult({...})` z mapowaniem (każde pole `?? null`):
   - `totalCostUsd` ← `total_cost_usd`
   - `inputTokens` ← `usage.input_tokens`
   - `outputTokens` ← `usage.output_tokens`
   - `durationMs` ← `duration_ms`
   - `raw` ← cały event. Return.
5. Wszystko inne → `onUnknown(event)`.

Uwaga semantyczna: `onTextDelta` mimo nazwy NIE dostaje delt per-token, tylko całe bloki tekstu z eventów `assistant` (granularność = content block per event JSONL). Eventy typu `user` (tool results), `system` inne niż init itd. lecą do `onUnknown` i są w praktyce ignorowane.

---

## 6. Obsługa błędów i exit codes (warstwa callerów)

`runClaude` sam nie interpretuje exit code. Wzorzec u wszystkich callerów:

- `exitCode !== 0` → throw. Dokładne komunikaty:
  - draft/compose/format: `` `claude exited with code ${exitCode}: ${stderr.slice(0, 500)}` ``
  - asystent: `` `claude exited ${exitCode}: ${stderr.slice(0, 300)}` `` (tylko gdy turn NIE był cancelled)
- pusta odpowiedź (akumulowany tekst `acc.trim()` pusty) → throw:
  - draft/compose: `'claude returned empty draft'`
  - format: `'claude returned empty result'`
  - asystent: `'empty response'`
- `onParseError` w draft-orchestratorze: tylko `log.warn` z `err.message` + pierwszymi 200 znakami linii. Pozostałe orchestratory nie podpinają `onParseError` (błędne linie znikają po cichu).
- błędy lecą dalej: draft → status sesji `error` w DB + WS broadcast; asystent → WS `{ type: 'assistant:error', conversationId, error: message }`.

`exitCode` może być `null` (Node: proces zabity sygnałem). Caller asystenta nie wpada wtedy w throw, bo przy cancel sprawdzanie exit code jest pomijane (sekcja 7).

---

## 7. Kill / cancel

Jedyny use-case z cancel: asystent (`assistant-orchestrator.ts`).

- Moduł trzyma mapę `activeTurns: Map<conversationId, { child: ChildProcess, cancelled: boolean }>` ("Active Claude subprocesses indexed by conversationId, for abort").
- `onSpawn: (child) => activeTurns.set(conversationId, { child, cancelled: false })`.
- `cancelTurn(conversationId, authAccountId)`:
  1. weryfikacja ownership konwersacji w DB (brak / cudza → `false`),
  2. brak wpisu w mapie → `false`,
  3. `entry.cancelled = true`,
  4. `entry.child.kill('SIGTERM')` w try/catch (fail → tylko `log.warn('kill failed: ...')`),
  5. return `true`.
- Po zamknięciu procesu pętla turn sprawdza `cancelled`:
  - cancelled → POMIJA walidację exit code i pustej odpowiedzi; częściowy tekst jest persystowany jako normalna wiadomość: usuwa się ewentualny niedokończony blok ```` ```action ```` (`stripActionBlock`), treść = `` `${strippedText.trim()}\n\n_(przerwane)_` ``, a gdy pusto: `'_(przerwane)_'`; `proposal = null`; broadcast `assistant:complete`.
  - nie-cancelled → normalna walidacja.
- `finally`: `activeTurns.delete(conversationId)` + `release()` semafora.

Pozostałe orchestratory (draft, compose, format) nie przekazują `onSpawn` - tych procesów nie da się przerwać.

---

## 8. Semafor współbieżności

`core/util/semaphore.ts` - prosty semafor in-process, FIFO:

- `new Semaphore(capacity)`; `acquire(): Promise<() => void>` - jeśli `available > 0`, dekrementuje i zwraca funkcję release; inaczej dokleja się do kolejki `waiters` i czeka.
- `release()`: jeśli ktoś czeka - `waiters.shift()()` (przekazanie slotu, `available` się nie zmienia); inaczej `available += 1`.
- getter `queued` = długość kolejki.
- Komentarz z kodu: "Used to throttle parallel `claude` CLI spawns (limit on Max subscription)."

**KLUCZOWE**: semafor NIE jest globalny. Każdy z 4 orchestratorów tworzy WŁASNĄ instancję module-level:

```ts
const sem = new Semaphore(env.CLAUDE_MAX_CONCURRENT);
```

(osobno w draft-, compose-, format- i assistant-orchestrator). Realny limit współbieżnych procesów `claude` = `4 × CLAUDE_MAX_CONCURRENT` (domyślnie 4×2 = 8), nie `CLAUDE_MAX_CONCURRENT`.

Wzorzec użycia: `const release = await sem.acquire(); try { ...runClaude... } finally { release(); }`. Semafor obejmuje cały czas życia procesu (nie tylko spawn). U asystenta `acquire` jest fire-and-forget (`void sem.acquire().then(async (release) => {...})`), bo HTTP route zwraca 202 zanim proces ruszy, a tokeny lecą po WS.

---

## 9. Use-case'y - parametry `runClaude` per orchestrator

| Use-case | sessionId | resume | appendSystemPrompt | model | onSpawn | handlery |
|---|---|---|---|---|---|---|
| **Draft DM** (`draft-orchestrator.generateInitialDraft`) | świeży `randomUUID()` przy KAŻDYM generowaniu (rotacja, patrz niżej) | parametr istnieje w `runStreaming`, ale żaden caller go nie używa (martwa ścieżka) | `composeSystemPrompt(account.systemPrompt, metaPrompt)` | `(await getDraftModel()) ?? env.DRAFT_MODEL` | brak | `onTextDelta` → WS `draft:token` (z `threadId`, `iterationKind`), `onToolUse` → WS `draft:tool_use`, `onResult`, `onParseError` → log.warn |
| **Compose (cold opener)** (`compose-orchestrator.generateComposeDraft`) | `randomUUID()` | - | `composeSystemPrompt(account.systemPrompt, await getGlobalMetaPrompt())` | `(await getDraftModel()) ?? env.DRAFT_MODEL` | brak | `onTextDelta` (tylko akumulacja, bez WS), `onResult` |
| **Format / polish** (`format-orchestrator.runFormatting`) | `randomUUID()` | - | `composeFormatSystemPrompt(persona, metaPrompt, formatPrompt)` | `(await getFormatModel()) ?? env.POLISH_MODEL` | brak | `onTextDelta` (akumulacja), `onResult` |
| **Asystent (chat)** (`assistant-orchestrator`) | `randomUUID()` per turn | - | `ASSISTANT_SYSTEM_PROMPT` (stała w kodzie) | `env.DRAFT_MODEL` (bez DB!) | tak → mapa `activeTurns`, cancel SIGTERM | `onTextDelta` → akumulacja + WS `{ type: 'assistant:token', conversationId, chunk }`, `onResult` |

Wspólne dla wszystkich: `onResult` mapuje `tokensUsed = inputTokens + outputTokens` gdy oba nie-null, inaczej `null`; `costUsd` zapisywany do DB jako string (`toString()`).

Rotacja sesji w draftach - komentarz z kodu (dosłownie):

> Rotate Claude session — `claude --session-id <existing-uuid>` hangs/fails because the session already exists on disk.

Czyli mimo że DB trzyma `claudeSessionId` per wątek, przy każdym `generateInitialDraft` generowany jest nowy UUID i zapisywany do DB; `--resume` w praktyce nieużywany.

Knowledge base (KB) NIGDY nie idzie przez `--append-system-prompt` - komentarz z kodu: "Knowledge base goes first as a stable prefix in the stdin prompt (never --append-system-prompt; that's argv-bounded by ARG_MAX)." KB jest doklejany na początek stdin-prompta jako `` `${kbBlock}\n\n---\n\n${basePrompt}` ``.

---

## 10. Health-check CLI (`core/health/claude-health.ts`)

Dodatkowe, niezależne od `runClaude` spawny binarki (jedyne miejsca z timeoutami):

1. **binary**: `fs access` na `env.CLAUDE_BIN_PATH`; fail → detail `` `binary not found at ${path}` ``.
2. **credentials**: istnienie `~/.claude/.credentials.json`; brak → `'credentials file missing — run `claude login`'`; rozmiar < 50 bajtów → `` `credentials file suspiciously small (${size} bytes)` ``; ok → detail `` `${size} bytes` ``.
3. **version**: `spawn(CLAUDE_BIN_PATH, ['--version'], { timeout: 5000 })`; exit ≠ 0 → `` `exit ${code}: ${(stderr || stdout).slice(0, 120).trim()}` ``; ok → `stdout.trim().slice(0, 60)`.
4. **deepProbe** (tylko na żądanie, `opts.deep`): realny round-trip LLM,
   `spawn(CLAUDE_BIN_PATH, ['--print', '--model', 'claude-haiku-4-5', '--input-format', 'text'], { timeout: 20_000 })`, stdin = `'ping'` (`child.stdin?.end('ping')`). Pusty output → `'claude returned empty output'`; exit ≠ 0 → `` `exit ${code}: ${(stderr || stdout).slice(0, 200).trim()}` ``; ok → `out.slice(0, 60)`.
   **Cache wyniku 1h** (`DEEP_PROBE_CACHE_MS = 60 * 60 * 1000`, module-level), w odpowiedzi pola `cachedFor` (sekundy od ostatniego runu, `Math.round((now - at) / 1000)`) i `lastRunAt` (ISO string). `result.ok` = AND wszystkich checków (z deepProbe gdy `deep`).

Checki 1-3 lecą równolegle (`Promise.all`). Struktura odpowiedzi: `{ ok, checks: { binary, credentials, version, deepProbe? } }`, każdy check `{ ok, detail? }`.

---

## Uwagi dla portu na FastAPI

1. **`exitCode: null` vs Python.** Node daje `close(code=null)` gdy proces zabity sygnałem; w Pythonie `proc.returncode` przy SIGTERM to `-15` (nigdy `None` po zakończeniu). Trzeba zmapować: `returncode < 0` → traktuj jak Node'owe `null` (cancel path). Asystent polega na tym, że przy cancel NIE rzuca błędu exit code - jeśli port potraktuje `-15` jako "exit ≠ 0" bez sprawdzenia flagi `cancelled`, cancelled turn wpadnie w error zamiast w `_(przerwane)_`.
2. **Brak timeoutu w `runClaude` to świadome zachowanie.** Nie dodawaj `asyncio.wait_for` "dla bezpieczeństwa" przy porcie 1:1 - zawieszony proces ma wisieć (jedyny kill to cancel asystenta). Timeouty mają tylko health-checki (5 s / 20 s).
3. **Prompt stdinem, nie argv.** `--disallowedTools` jest variadic i zjada positional prompt; poza tym `--append-system-prompt` w argv jest ograniczony ARG_MAX, dlatego KB idzie w stdin. W Pythonie: `proc.communicate(input=prompt.encode())` albo write+close stdin; pamiętaj o ignorowaniu `BrokenPipeError` (odpowiednik no-op handlera na `stdin.error`).
4. **Env procesu = pełne `process.env`.** Subprocess musi widzieć `HOME` (credentials CLI) i `PATH`. W FastAPI w Dockerze: nie podawaj `env=` z okrojonym dictem.
5. **Semafor jest per moduł, nie globalny.** 4 niezależne instancje × `CLAUDE_MAX_CONCURRENT` (default 2) = realnie do 8 procesów. Jeden globalny `asyncio.Semaphore` zmieniłby throughput. Do portu 1:1: osobny semafor per serwis. Semafor jest FIFO (kolejka waiters) - `asyncio.Semaphore` w CPythonie też budzi w kolejności FIFO, ok.
6. **Parser musi być odporny na partial lines i ostatnią linię bez `\n`** (`flush()` po EOF). Czytanie `readline()` w asyncio załatwia split, ale pamiętaj o trim, pomijaniu pustych linii i o tym, że błędna linia JSON NIE przerywa parsowania (callback `onParseError` + continue).
7. **Casing pól**: eventy CLI są snake_case (`session_id`, `total_cost_usd`, `usage.input_tokens`, `usage.output_tokens`, `duration_ms`), wewnętrzny wynik parsera camelCase (`totalCostUsd`, `inputTokens`...). Jeśli wynik parsera wycieka dalej (WS/DB), zachowaj nazwy z warstwy, którą portujesz, nie z CLI.
8. **`onTextDelta` to nie delty.** To całe bloki `text` z eventów `assistant`. Frontend po WS dostaje chunki tej granulacji - nie zmieniaj na char-streaming ani nie skleaj w jedną wiadomość.
9. **Kolejność flag CLI**: trzymaj dokładnie tę kolejność z sekcji 3 (zwłaszcza `--disallowedTools` przed `--input-format text` i brak positionala). `--verbose` jest wymagany przez CLI przy `--print` + `--output-format stream-json` (bez niego CLI odmawia stream-json), nie usuwaj.
10. **`--session-id` z istniejącym UUID wiesza/wywala CLI** (sesja już na dysku). Dlatego drafty rotują UUID przy każdym generowaniu, a `--resume` jest de facto martwy. Port musi zachować rotację.
11. **stderr buforowany w całości w pamięci** i obcinany dopiero w komunikatach błędów (300/500 znaków). Przy bardzo gadatliwym stderr to potencjalny memory growth - 1:1 znaczy tak samo.
12. **Koszt jako string**: `costUsd` idzie do DB przez `Number.prototype.toString()`. `str(float)` w Pythonie daje czasem inną reprezentację (np. wykładniczą dla bardzo małych wartości JS `1e-7`); jeśli DB ma kolumnę numeric/decimal, najbezpieczniej przekazać `Decimal(str(value))` i nie porównywać stringów.
13. **Kill = SIGTERM, pojedynczy strzał.** Brak eskalacji do SIGKILL i brak czekania na śmierć - `cancelTurn` zwraca `true` od razu po `kill()`. Port: `proc.terminate()` bez `wait_for` + flaga `cancelled` w mapie per `conversationId`.
14. **Deep probe health-checka ma cache 1h w pamięci procesu** (module-level zmienna). W FastAPI z wieloma workerami (uvicorn workers > 1) cache będzie per-worker - admin działa na 1 procesie, więc dla 1:1 trzymaj 1 workera albo przenieś cache świadomie.
