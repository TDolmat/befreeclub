import { Badge } from '@/core/components/ui/Badge';
import type { BadgeVariant } from '@/core/components/ui/Badge';
import { Callout } from '@/core/components/ui/Callout';
import { SettingsRow } from '@/core/components/ui/SettingsRow';
import { Button } from '@/core/components/ui/button';
import { Input } from '@/core/components/ui/input';
import { useToast } from '@/core/components/ui/toast';
import {
  useClearConnectionSecret,
  useConnections,
  useSetConnectionSecret,
  useTestConnection,
} from '@/core/hooks/useSettings';
import { settingsApi } from '@/core/lib/settings-api';
import { resolveConnectionError, resolveSmartError } from '@/core/lib/settings-errors';
import type { ConnectionResult, ConnectionSource, ConnectionStatus } from '@bfc/shared';
import { Eye, EyeOff, Loader2, RefreshCw, Save, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';

/**
 * Połączenia API. Dwie klasy wierszy:
 *
 * 1. editable=true (openai/resend/sender/metaCapi) - klucz edytowalny w panelu.
 *    Pokazujemy maskę efektywnej wartości (conn.masked) + Badge źródła
 *    (z panelu / z env / brak), input na NOWY klucz + Zapisz, przycisk Pokaż
 *    (reveal pełnej wartości na żądanie, nigdy w stanie globalnym) i Usuń
 *    (powrót do env, tylko gdy source==='panel'). Plus stary Testuj.
 *
 * 2. editable=false (stripe/circle) - status-only jak dotąd: Badge statusu +
 *    Testuj. Sekret żyje w env, panel go nie dotyka.
 *
 * status 'mock' (dev) i 'skipped' (Sender/Meta - brak taniego testu) NIE straszą:
 * neutralna/info plakietka, bez czerwieni.
 */

const STATUS_META: Record<ConnectionStatus, { label: string; variant: BadgeVariant }> = {
  ok: { label: 'działa', variant: 'success' },
  error: { label: 'błąd', variant: 'error' },
  unconfigured: { label: 'brak klucza', variant: 'muted' },
  skipped: { label: 'ustawiony', variant: 'info' },
  mock: { label: 'mock (dev)', variant: 'info' },
};

const SOURCE_META: Record<ConnectionSource, { label: string; variant: BadgeVariant }> = {
  panel: { label: 'z panelu', variant: 'brand' },
  env: { label: 'z env', variant: 'info' },
  brak: { label: 'brak', variant: 'muted' },
};

export function ConnectionsSection() {
  const query = useConnections();
  const test = useTestConnection();

  if (query.isLoading) {
    return (
      <div className="flex items-center justify-center gap-2 py-12 text-foreground/40">
        <Loader2 className="h-4 w-4 animate-spin" /> Ładuję status połączeń…
      </div>
    );
  }

  if (query.isError) {
    const err = resolveSmartError(query.error);
    return (
      <Callout
        variant="error"
        title={err.title}
        description={
          <>
            {err.hint}
            {err.rawDetail && (
              <details className="mt-1.5">
                <summary className="cursor-pointer text-foreground/60 hover:text-foreground/80">
                  Szczegóły techniczne
                </summary>
                <code className="mt-1 block break-words rounded bg-foreground/5 px-2 py-1 text-[11px] text-foreground/60">
                  {err.rawDetail}
                </code>
              </details>
            )}
          </>
        }
        actionLabel="Spróbuj ponownie"
        onAction={() => query.refetch()}
      />
    );
  }

  const connections = query.data?.connections ?? [];

  return (
    <div className="flex flex-col gap-3">
      {connections.map((conn) =>
        conn.editable ? (
          <EditableConnectionRow
            key={conn.key}
            conn={conn}
            testing={test.isPending && test.variables === conn.key}
            onTest={() => test.mutate(conn.key)}
          />
        ) : (
          <ConnectionRow
            key={conn.key}
            conn={conn}
            testing={test.isPending && test.variables === conn.key}
            onTest={() => test.mutate(conn.key)}
          />
        ),
      )}
    </div>
  );
}

/**
 * Wiersz edytowalnego klucza API (4 sekrety). Maska + źródło + edycja + reveal.
 * Pełna wartość żyje tylko w lokalnym revealed (po kliknięciu Pokaż), nigdy w
 * cache react-query ani w stanie globalnym. Wpisywany nowy klucz w draft.
 */
function EditableConnectionRow({
  conn,
  testing,
  onTest,
}: {
  conn: ConnectionResult;
  testing: boolean;
  onTest: () => void;
}) {
  const { toast } = useToast();
  const setSecret = useSetConnectionSecret();
  const clearSecret = useClearConnectionSecret();

  const [draft, setDraft] = useState('');
  const [revealed, setRevealed] = useState<string | null>(null);
  const [revealing, setRevealing] = useState(false);

  const source = SOURCE_META[conn.source];
  const saving = setSecret.isPending;
  const clearing = clearSecret.isPending;
  const hasValue = conn.source !== 'brak';

  const handleSave = () => {
    const value = draft.trim();
    if (!value) return;
    setSecret.mutate(
      { key: conn.key, value },
      {
        onSuccess: () => {
          setDraft('');
          setRevealed(null);
        },
      },
    );
  };

  const handleReveal = async () => {
    if (revealed !== null) {
      setRevealed(null);
      return;
    }
    setRevealing(true);
    try {
      const res = await settingsApi.revealConnectionSecret(conn.key);
      setRevealed(res.value ?? '');
    } catch (err) {
      const e = resolveSmartError(err);
      toast({ kind: 'error', title: e.title, description: e.hint });
    } finally {
      setRevealing(false);
    }
  };

  const handleClear = () => {
    clearSecret.mutate(conn.key, {
      onSuccess: () => {
        setRevealed(null);
        setDraft('');
      },
    });
  };

  return (
    <SettingsRow
      fieldId={conn.key}
      label={conn.label}
      description="Klucz nadpisuje wartość z env. Pusto = używana jest wartość z env."
      badge={<Badge variant={source.variant}>{source.label}</Badge>}
      stacked
    >
      <div className="flex flex-col gap-2.5">
        {/* Maska / odsłonięta wartość efektywna */}
        <div className="flex items-center gap-2">
          <code className="min-w-0 flex-1 truncate rounded-md border border-foreground/10 bg-foreground/[0.04] px-3 py-2 font-mono text-[12px] text-foreground/70">
            {revealed !== null
              ? revealed || '(pusto)'
              : hasValue
                ? (conn.masked ?? '••••')
                : 'brak klucza'}
          </code>
          {hasValue && (
            <Button
              variant="ghost"
              size="sm"
              onClick={handleReveal}
              disabled={revealing}
              title={revealed !== null ? 'Ukryj wartość' : 'Pokaż pełną wartość'}
            >
              {revealing ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : revealed !== null ? (
                <EyeOff className="h-4 w-4" />
              ) : (
                <Eye className="h-4 w-4" />
              )}
              {revealed !== null ? 'Ukryj' : 'Pokaż'}
            </Button>
          )}
        </div>

        {/* Wpisanie nowego klucza */}
        <div className="flex items-center gap-2">
          <Input
            type="password"
            autoComplete="off"
            spellCheck={false}
            aria-label={`Nowy klucz: ${conn.label}`}
            placeholder="Wklej nowy klucz…"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && draft.trim()) handleSave();
            }}
            className="font-mono"
          />
          <Button
            variant="default"
            size="sm"
            onClick={handleSave}
            disabled={!draft.trim() || saving}
            title="Zapisz nowy klucz"
          >
            {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            Zapisz
          </Button>
        </div>

        {/* Akcje: Usuń (tylko z panelu) + Testuj */}
        <div className="flex items-center justify-end gap-1.5">
          {conn.source === 'panel' && (
            <Button
              variant="ghost"
              size="sm"
              onClick={handleClear}
              disabled={clearing}
              title="Usuń klucz z panelu, wróć do wartości z env"
            >
              {clearing ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Trash2 className="h-4 w-4" />
              )}
              Usuń (wróć do env)
            </Button>
          )}
          {conn.configured ? (
            <Button
              variant="ghost"
              size="sm"
              onClick={onTest}
              disabled={testing}
              title="Testuj połączenie"
            >
              {testing ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <RefreshCw className="h-4 w-4" />
              )}
              Testuj
            </Button>
          ) : (
            <Badge variant="muted">brak testu</Badge>
          )}
        </div>
      </div>
    </SettingsRow>
  );
}

function ConnectionRow({
  conn,
  testing,
  onTest,
}: {
  conn: ConnectionResult;
  testing: boolean;
  onTest: () => void;
}) {
  const meta = STATUS_META[conn.status];
  const smart = resolveConnectionError(conn.key, conn.status, conn.detail);

  // Circle: community ID edytujesz w Analityce, tu tylko status (DECYZJA #4).
  const circleNote = conn.key === 'circle';

  return (
    <SettingsRow
      fieldId={conn.key}
      label={conn.label}
      // Przy błędzie detal idzie do Callouta (rawDetail), tu nie dublujemy.
      description={smart ? undefined : conn.detail || undefined}
      badge={<Badge variant={meta.variant}>{meta.label}</Badge>}
      error={
        smart
          ? {
              variant: conn.status === 'unconfigured' ? 'warning' : 'error',
              title: smart.title,
              description: (
                <>
                  {smart.hint}
                  {smart.rawDetail && (
                    <details className="mt-1.5">
                      <summary className="cursor-pointer text-foreground/50 hover:text-foreground/70">
                        Szczegóły techniczne
                      </summary>
                      <code className="mt-1 block break-words rounded bg-foreground/5 px-2 py-1 text-[11px] text-foreground/60">
                        {smart.rawDetail}
                      </code>
                    </details>
                  )}
                </>
              ),
              actionRoute: smart.actionRoute,
              actionLabel: smart.actionLabel,
            }
          : null
      }
    >
      <div className="flex flex-col items-end gap-1.5">
        {conn.configured ? (
          <Button
            variant="ghost"
            size="sm"
            onClick={onTest}
            disabled={testing}
            title="Testuj połączenie"
          >
            {testing ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <RefreshCw className="h-4 w-4" />
            )}
            Testuj
          </Button>
        ) : (
          <Badge variant="muted">brak testu</Badge>
        )}
        {circleNote && (
          <Link
            to="/ustawienia/analytics?focus=circleCommunityId"
            className="text-[11px] text-foreground/60 underline-offset-4 hover:text-foreground/80 hover:underline"
          >
            Community ID w Analityce
          </Link>
        )}
      </div>
    </SettingsRow>
  );
}
