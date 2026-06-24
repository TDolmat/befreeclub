import { Badge } from '@/core/components/ui/Badge';
import { Callout } from '@/core/components/ui/Callout';
import { SettingsRow } from '@/core/components/ui/SettingsRow';
import { Button } from '@/core/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/core/components/ui/dialog';
import { Switch } from '@/core/components/ui/switch';
import { useAdminSettings, useRunCleanup, useSaveSettingsGroup } from '@/core/hooks/useSettings';
import { cn } from '@/core/lib/utils';
import type { ToggleState } from '@bfc/shared';
import { AlertTriangle, Loader2, Play } from 'lucide-react';
import { useState } from 'react';
import { TunableField } from './TunableField';

// DECYZJA #2: Switch OFF jest niewidoczny (bg-muted == --card). Wymuszamy kontrast lokalnie.
// disabled:opacity-70 - OFF nie znika podczas zapisu (save.isPending), zostaje czytelny.
const SWITCH_VISIBLE = 'data-[state=unchecked]:bg-foreground/20 disabled:opacity-70';

// Self-contained: własny hook, bez propsów. Prop {data} jeszcze przyjmuje call
// site (SettingsCategoryPage) - ignorujemy go, faza Routing zdejmie przekazywanie.
export function MembershipSection() {
  const { data } = useAdminSettings();
  const save = useSaveSettingsGroup('membership');
  const runCleanup = useRunCleanup();
  const [confirmLive, setConfirmLive] = useState(false);

  if (!data) return null;
  const m = data.groups.membership;
  const cleanup = m.cleanup;
  const inShadow = cleanup.dryRun ?? true;

  // Włączanie REALNEGO usuwania (enabled=true + dryRun=false) wymaga potwierdzenia.
  const confirmGoLive = () => {
    save.mutate({ cleanup: { enabled: true, dryRun: false } });
    setConfirmLive(false);
  };

  // Wrapper flex flex-col gap-3 daje już SettingsCategoryPage - tu Fragment.
  return (
    <>
      {/* Cleanup - destrukcyjny. Switch włącza zawsze w trybie cienia. */}
      <SettingsRow
        fieldId="cleanup"
        label="Automatyczny cleanup członkostw"
        badge={<Badge variant="error">destrukcyjny</Badge>}
        description="Usuwa z Circle ludzi bez aktywnej subskrypcji. Włączony liczy i decyduje automatycznie w tle. Świeży deploy nikogo nie rusza."
      >
        <Switch
          aria-label="Automatyczny cleanup członkostw"
          className={SWITCH_VISIBLE}
          checked={cleanup.enabled}
          disabled={save.isPending}
          onCheckedChange={(on) =>
            // Główny switch zawsze wraca do trybu cienia (dryRun=true) - i przy
            // włączaniu, i przy wyłączaniu. Realne usuwanie (dryRun=false) wchodzi
            // WYŁĄCZNIE przez Dialog confirmGoLive. Dzięki temu wyłączenie cleanupu
            // w trybie live nie zostawia ukrytego dryRun=false, którego "Odpal teraz" by użył.
            save.mutate({
              cleanup: { enabled: on, dryRun: true },
            })
          }
        />
      </SettingsRow>

      {cleanup.enabled && (
        <SettingsRow
          fieldId="cleanupDryRun"
          label="Tryb cienia (dry run)"
          description="Liczy i pokazuje kogo by usunął, nikogo nie rusza. Trzymaj włączony, dopóki nie potwierdzisz, że decyzje się zgadzają."
        >
          <Switch
            aria-label="Tryb cienia (dry run) cleanupu członkostw"
            className={SWITCH_VISIBLE}
            checked={inShadow}
            disabled={save.isPending}
            onCheckedChange={(stillDry) => {
              if (stillDry) {
                save.mutate({ cleanup: { enabled: true, dryRun: true } });
              } else {
                // Wyłączenie cienia = realne usuwanie. Tylko za potwierdzeniem.
                setConfirmLive(true);
              }
            }}
          />
        </SettingsRow>
      )}

      {cleanup.enabled && !inShadow && (
        <Callout
          variant="error"
          title="Realne usuwanie WŁĄCZONE"
          description="Cleanup naprawdę kasuje ludzi z Circle. Wróć do trybu cienia, jeśli to nie jest świadoma decyzja."
        />
      )}

      {/* Ręczny przebieg (respektuje dryRun, ignoruje enabled - świadoma akcja). */}
      <SettingsRow
        fieldId="cleanupRunNow"
        label="Odpal cleanup teraz"
        description="Jeden przebieg na żądanie. Tryb (cień lub realny) bierze z ustawień powyżej."
      >
        <Button
          variant="outline"
          size="sm"
          onClick={() => runCleanup.mutate()}
          disabled={runCleanup.isPending}
        >
          {runCleanup.isPending ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Play className="h-4 w-4" />
          )}
          Odpal teraz
        </Button>
      </SettingsRow>

      {runCleanup.data && <LastRun result={runCleanup.data} />}

      {/* Pozostałe workery - niedestrukcyjne. */}
      <ToggleRow
        fieldId="klarnaReconcile"
        label="Automatyczny reconcile Klarny"
        description="Nadaje dostęp po opłaceniu Klarną. Nie usuwa. Domyślnie wyłączony."
        state={m.klarnaReconcile}
        disabled={save.isPending}
        onChange={(enabled) => save.mutate({ klarnaReconcile: { enabled } })}
      />
      <ToggleRow
        fieldId="inviteRetry"
        label="Automatyczne ponawianie zaproszeń Circle"
        description="Ponawia tylko nieudane zaproszenia. Domyślnie wyłączony."
        state={m.inviteRetry}
        disabled={save.isPending}
        onChange={(enabled) => save.mutate({ inviteRetry: { enabled } })}
      />

      {/* Interwały - zmiana po restarcie backendu (TunableField pokazuje plakietkę restart). */}
      <TunableField
        id="cleanupIntervalMs"
        label="Interwał cleanupu"
        description="Co ile worker cleanupu robi przebieg. Zmiana zadziała po restarcie backendu."
        duration
        state={m.cleanupIntervalMs}
        saving={save.isPending}
        rule={{ min: 5000 }}
        onSave={(value) => save.mutate({ cleanupIntervalMs: { value } })}
      />
      <TunableField
        id="klarnaReconcileIntervalMs"
        label="Interwał reconcile Klarny"
        description="Co ile worker reconcile Klarny robi przebieg. Po restarcie backendu."
        duration
        state={m.klarnaReconcileIntervalMs}
        saving={save.isPending}
        rule={{ min: 5000 }}
        onSave={(value) => save.mutate({ klarnaReconcileIntervalMs: { value } })}
      />
      <TunableField
        id="inviteRetryIntervalMs"
        label="Interwał ponawiania zaproszeń"
        description="Co ile worker ponawia nieudane zaproszenia. Po restarcie backendu."
        duration
        state={m.inviteRetryIntervalMs}
        saving={save.isPending}
        rule={{ min: 5000 }}
        onSave={(value) => save.mutate({ inviteRetryIntervalMs: { value } })}
      />

      <Dialog open={confirmLive} onOpenChange={setConfirmLive}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-destructive">
              <AlertTriangle className="h-5 w-5" />
              Wyłączyć tryb cienia?
            </DialogTitle>
            <DialogDescription className="pt-1">
              To zacznie REALNIE usuwać ludzi z Circle. Cleanup przestanie tylko liczyć i naprawdę
              skasuje członków bez aktywnej subskrypcji. Na pewno?
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" size="sm" onClick={() => setConfirmLive(false)}>
              Zostaw tryb cienia
            </Button>
            <Button variant="destructive" size="sm" onClick={confirmGoLive}>
              Tak, włącz realne usuwanie
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

function ToggleRow({
  fieldId,
  label,
  description,
  state,
  disabled,
  onChange,
}: {
  fieldId: string;
  label: string;
  description: string;
  state: ToggleState;
  disabled: boolean;
  onChange: (enabled: boolean) => void;
}) {
  return (
    <SettingsRow fieldId={fieldId} label={label} description={description}>
      <Switch
        aria-label={label}
        className={SWITCH_VISIBLE}
        checked={state.enabled}
        disabled={disabled}
        onCheckedChange={onChange}
      />
    </SettingsRow>
  );
}

function LastRun({
  result,
}: {
  result: { checked: number; wouldRemove: number; removed: number; dryRun: boolean };
}) {
  return (
    <div
      className={cn(
        'rounded-lg border px-4 py-3 text-[12px] text-foreground/70',
        'border-foreground/10 bg-foreground/[0.03]',
      )}
    >
      <span className="font-semibold text-foreground/90">Ostatni przebieg: </span>
      tryb {result.dryRun ? 'cienia' : 'realny'}, sprawdzonych {result.checked},
      {result.dryRun
        ? ` kandydatów do usunięcia ${result.wouldRemove}`
        : ` usuniętych ${result.removed}`}
      .
    </div>
  );
}
