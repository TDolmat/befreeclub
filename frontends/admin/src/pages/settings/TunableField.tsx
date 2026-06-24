import { Badge } from '@/core/components/ui/Badge';
import { SettingsRow } from '@/core/components/ui/SettingsRow';
import { Button } from '@/core/components/ui/button';
import { Input } from '@/core/components/ui/input';
import { cn } from '@/core/lib/utils';
import type { TunableState } from '@bfc/shared';
import { Loader2, RotateCcw, Save } from 'lucide-react';
import { useEffect, useState } from 'react';

/**
 * Edytowalny skalar (TUNABLE) renderowany jako wiersz ustawień. Lewa strona
 * (label + opis) idzie z registry przez SettingsRow, prawa to Input + Zapisz +
 * Reset. Lokalny stan + dirty, zapis przez onSave.
 *
 * Tryb duration: wartość trzymana w backendzie w MS (kontrakt bez zmian), ale
 * w UI edytujesz ją jako rozbicie dni / godz / min / sek (jak czas w iPhone).
 * Nikt nie liczy 21600000 ms - widzi 0 dni 6 godz 0 min 0 sek. Wczytanie
 * rozbija ms na pola, zapis sumuje je z powrotem na ms.
 *
 * Kontrakt zachowany: pusta wartość = onSave(null) (powrót do fallbacku env),
 * number rzutowany przez Number, fieldId == camelCase klucz pola = DOM id wiersza.
 * Walidacja inline PRZED wysłaniem (zakresy jak w backendzie) - błędny zapis nie
 * leci do API, tylko pokazuje podpowiedź pod wierszem.
 */
export function TunableField({
  id,
  label,
  description,
  hint,
  state,
  type = 'text',
  duration = false,
  placeholder,
  saving,
  onSave,
  rule,
}: {
  /** camelCase klucz pola = DOM id wiersza (deep-link). */
  id: string;
  label: string;
  description?: string;
  /** Deprecated alias dla description (stare sekcje przed refactorem na registry). */
  hint?: string;
  state: TunableState;
  type?: 'text' | 'number';
  /** Pole czasu: edycja jako dni/godz/min/sek, do backendu leci w ms. */
  duration?: boolean;
  placeholder?: string;
  saving: boolean;
  onSave: (value: string | number | null) => void;
  /** Zakres walidacji inline (lustrzane do settings_catalog.py). */
  rule?: TunableRule;
}) {
  const desc = description ?? hint;
  const [text, setText] = useState('');
  const [parts, setParts] = useState<DurationParts>(EMPTY_PARTS);
  const [dirty, setDirty] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  // Efektywna wartość (db albo env) - na niej bazują pola czasu, żeby było widać
  // co realnie obowiązuje. Źródło (panel/env) i tak pokazuje SourceBadge.
  const effectiveMs = (state.value ?? state.envFallback) as number | null;

  // biome-ignore lint/correctness/useExhaustiveDependencies: reset stanu lokalnego ma zależeć od wartości z serwera, nie od pochodnych (effectiveMs liczone z tych samych pól).
  useEffect(() => {
    if (duration) {
      setParts(effectiveMs == null ? EMPTY_PARTS : msToParts(effectiveMs));
    } else {
      setText(state.value == null ? '' : String(state.value));
    }
    setDirty(false);
    setLocalError(null);
  }, [state.value, state.envFallback]);

  const setPart = (key: keyof DurationParts, value: string) => {
    setParts((prev) => ({ ...prev, [key]: value }));
    setDirty(true);
    if (localError) setLocalError(null);
  };

  const handleSaveDuration = () => {
    // Wszystkie pola puste = powrót do env (jak pusty input w trybie skalarnym).
    if (DURATION_KEYS.every((k) => parts[k].trim() === '')) {
      setLocalError(null);
      return onSave(null);
    }
    const nums: Record<keyof DurationParts, number> = { d: 0, h: 0, m: 0, s: 0 };
    for (const k of DURATION_KEYS) {
      const t = parts[k].trim();
      const n = t === '' ? 0 : Number(t);
      if (!Number.isInteger(n) || n < 0) {
        setLocalError('Wpisz całkowite, nieujemne wartości.');
        return;
      }
      nums[k] = n;
    }
    const ms = partsToMs(nums);
    if (rule?.min != null && ms < rule.min) {
      setLocalError(`Za krótko. Minimum to ${formatDuration(rule.min)}.`);
      return;
    }
    if (rule?.max != null && ms > rule.max) {
      setLocalError(`Za długo. Maksimum to ${formatDuration(rule.max)}.`);
      return;
    }
    setLocalError(null);
    onSave(ms);
  };

  const handleSaveScalar = () => {
    const trimmed = text.trim();
    if (trimmed === '') {
      setLocalError(null);
      return onSave(null);
    }
    const validation = validateTunable(trimmed, type, rule);
    if (validation) {
      setLocalError(validation);
      return;
    }
    setLocalError(null);
    if (type === 'number') return onSave(Number(trimmed));
    onSave(trimmed);
  };

  const handleSave = duration ? handleSaveDuration : handleSaveScalar;

  const handleReset = () => {
    setLocalError(null);
    onSave(null);
  };

  return (
    <SettingsRow
      fieldId={id}
      label={label}
      description={desc}
      badge={<SourceBadge source={state.source} requiresRestart={state.requiresRestart} />}
      stacked
      error={
        localError ? { variant: 'error', title: 'Popraw wartość', description: localError } : null
      }
    >
      <div className="flex items-center gap-2">
        {duration ? (
          <fieldset
            className="flex flex-wrap items-center gap-x-3 gap-y-2 m-0 min-w-0 border-0 p-0"
            aria-label={label}
          >
            {DURATION_SEGMENTS.map((seg) => (
              <div key={seg.key} className="flex items-center gap-1.5">
                <Input
                  id={`${id}-${seg.key}`}
                  type="number"
                  min={0}
                  inputMode="numeric"
                  aria-label={`${label}: ${seg.unit}`}
                  aria-invalid={!!localError}
                  className={cn(
                    'w-16 bg-background border-foreground/20 text-center',
                    localError && 'border-destructive/60',
                  )}
                  value={parts[seg.key]}
                  onChange={(e) => setPart(seg.key, e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && dirty && !saving) handleSave();
                  }}
                />
                <span className="text-xs text-foreground/60">{seg.unit}</span>
              </div>
            ))}
          </fieldset>
        ) : (
          <Input
            id={`${id}-input`}
            type={type}
            inputMode={type === 'number' ? 'numeric' : undefined}
            aria-label={label}
            aria-invalid={!!localError}
            // DECYZJA #2: mocniejszy kontrast niż domyślny Input (bg-background/40 + border-input).
            // placeholder:text-foreground/60 - czytelniejsza realna wartość z env niż globalne /40.
            className={cn(
              'bg-background border-foreground/20 placeholder:text-foreground/60',
              localError && 'border-destructive/60',
            )}
            value={text}
            placeholder={
              placeholder ?? (state.envFallback != null ? `${state.envFallback} (z env)` : '')
            }
            onChange={(e) => {
              setText(e.target.value);
              setDirty(true);
              if (localError) setLocalError(null);
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && dirty && !saving) handleSave();
            }}
          />
        )}
        <Button
          variant="default"
          size="icon"
          // bez poświaty - przy ~8 polach w kategorii ciągłe shadow-glow to jarmark; żółte tło + ikona wystarczą.
          className="shadow-none hover:shadow-none"
          title="Zapisz"
          onClick={handleSave}
          disabled={!dirty || saving}
        >
          {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
        </Button>
        {state.source === 'db' && (
          <Button
            variant="ghost"
            size="icon"
            title="Wróć do wartości z env"
            onClick={handleReset}
            disabled={saving}
          >
            <RotateCcw className="h-4 w-4" />
          </Button>
        )}
      </div>
    </SettingsRow>
  );
}

/** Zakres walidacji inline - lustro reguł z settings_catalog.py. */
export interface TunableRule {
  min?: number;
  max?: number;
  /** Wymaga protokołu (://) - jak _url_str w backendzie. */
  url?: boolean;
}

function validateTunable(
  trimmed: string,
  type: 'text' | 'number',
  rule?: TunableRule,
): string | null {
  if (type === 'number') {
    const n = Number(trimmed);
    if (!Number.isInteger(n)) return 'Wpisz liczbę całkowitą.';
    if (rule?.min != null && n < rule.min) return `Wartość musi być >= ${rule.min}.`;
    if (rule?.max != null && n > rule.max) return `Wartość musi być <= ${rule.max}.`;
    return null;
  }
  if (rule?.url && !trimmed.includes('://')) {
    return 'Wpisz pełny adres z protokołem, np. https://befreeclub.pl.';
  }
  return null;
}

// ── Czas: ms <-> dni / godz / min / sek (rozbicie jak w iPhone) ──────────────

interface DurationParts {
  d: string;
  h: string;
  m: string;
  s: string;
}

const EMPTY_PARTS: DurationParts = { d: '', h: '', m: '', s: '' };
const DURATION_KEYS = ['d', 'h', 'm', 's'] as const;

const DURATION_SEGMENTS: { key: keyof DurationParts; unit: string }[] = [
  { key: 'd', unit: 'dni' },
  { key: 'h', unit: 'godz' },
  { key: 'm', unit: 'min' },
  { key: 's', unit: 'sek' },
];

const MS = { d: 86_400_000, h: 3_600_000, m: 60_000, s: 1_000 } as const;

/** ms -> pola dni/godz/min/sek (zaokrąglone do sekund). */
function msToParts(ms: number): DurationParts {
  let total = Math.max(0, Math.round(ms / 1000)); // sekundy
  const s = total % 60;
  total = Math.floor(total / 60);
  const m = total % 60;
  total = Math.floor(total / 60);
  const h = total % 24;
  const d = Math.floor(total / 24);
  return { d: String(d), h: String(h), m: String(m), s: String(s) };
}

function partsToMs(p: Record<keyof DurationParts, number>): number {
  return p.d * MS.d + p.h * MS.h + p.m * MS.m + p.s * MS.s;
}

/** ms -> czytelny zapis dla komunikatów, np. 5000 -> "5 sek", 5400000 -> "1 godz 30 min". */
function formatDuration(ms: number): string {
  const p = msToParts(ms);
  const out: string[] = [];
  for (const seg of DURATION_SEGMENTS) {
    const n = Number(p[seg.key]);
    if (n > 0) out.push(`${n} ${seg.unit}`);
  }
  return out.length ? out.join(' ') : '0 sek';
}

function SourceBadge({
  source,
  requiresRestart,
}: {
  source: TunableState['source'];
  requiresRestart: boolean;
}) {
  const labelMap = { db: 'panel', env: 'env', default: 'default' } as const;
  return (
    <span className="flex items-center gap-1.5">
      {requiresRestart && (
        <Badge variant="warning" title="Zmiana zadziała po restarcie backendu">
          restart
        </Badge>
      )}
      <Badge variant={source === 'db' ? 'brand' : 'muted'}>{labelMap[source]}</Badge>
    </span>
  );
}
