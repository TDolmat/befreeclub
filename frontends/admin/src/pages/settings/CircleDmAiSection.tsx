import { SettingsRow } from '@/core/components/ui/SettingsRow';
import { useAdminSettings, useSaveSettingsGroup } from '@/core/hooks/useSettings';
import type { CircleDmAiGroup } from '@bfc/shared';
import { ExternalLink } from 'lucide-react';
import { Link } from 'react-router-dom';
import { TunableField } from './TunableField';

/**
 * Sekcja Circle DM & AI jako lista wierszy. Nagłówek kategorii rysuje
 * SettingsCategoryPage z registry - tu renderujemy tylko wiersze.
 *
 * Prompty, modele draft/format i progi wątków NIE są tutaj - linkujemy do
 * /circle-dm/settings, żeby nie duplikować źródła prawdy.
 *
 * fieldId każdego wiersza == camelCase klucz pola z @bfc/shared = DOM id =
 * cel deep-linku ?focus=fieldId (zgodne z settings-registry.ts).
 */
export function CircleDmAiSection() {
  const { data } = useAdminSettings();
  const save = useSaveSettingsGroup('circleDmAi');
  const onSave = (key: keyof CircleDmAiGroup) => (value: string | number | null) =>
    save.mutate({ [key]: { value } });

  if (!data) return null;
  const g = data.groups.circleDmAi;

  return (
    <>
      <SettingsRow
        fieldId="circleDmPrompts"
        label="Prompty i modele Circle DM"
        description="Prompty, modele draft/format i progi wątków masz na osobnej stronie. Nie dublujemy ich tutaj."
      >
        <Link
          to="/circle-dm/settings"
          className="inline-flex items-center gap-1.5 rounded-md border border-foreground/20 bg-background px-3 py-1.5 text-sm font-medium text-foreground transition-colors hover:bg-foreground/5"
        >
          Ustawienia Circle DM
          <ExternalLink className="h-4 w-4" />
        </Link>
      </SettingsRow>

      <TunableField
        id="claudeMaxConcurrent"
        label="Max równoległych zapytań Claude"
        type="number"
        description="1-8. Semafor tworzony przy starcie, zmiana zadziała po restarcie. Realny limit to ok. 4x ta wartość (jeden request bywa kilkoma wywołaniami), więc 2 = ok. 8 naraz."
        rule={{ min: 1, max: 8 }}
        state={g.claudeMaxConcurrent}
        saving={save.isPending}
        onSave={onSave('claudeMaxConcurrent')}
      />
      <TunableField
        id="pollingIntervalMs"
        label="Interwał pollingu Circle"
        duration
        description="Co ile odpytujemy Circle o nowe wiadomości. Po restarcie backendu."
        rule={{ min: 5000 }}
        state={g.pollingIntervalMs}
        saving={save.isPending}
        onSave={onSave('pollingIntervalMs')}
      />
      <TunableField
        id="voiceTranscriptIntervalMs"
        label="Interwał transkrypcji głosówek"
        duration
        description="Co ile worker transkrybuje głosówki. Po restarcie backendu."
        rule={{ min: 5000 }}
        state={g.voiceTranscriptIntervalMs}
        saving={save.isPending}
        onSave={onSave('voiceTranscriptIntervalMs')}
      />
      <TunableField
        id="imageDescriptionIntervalMs"
        label="Interwał opisu obrazków"
        duration
        description="Co ile worker opisuje obrazki. Po restarcie backendu."
        rule={{ min: 5000 }}
        state={g.imageDescriptionIntervalMs}
        saving={save.isPending}
        onSave={onSave('imageDescriptionIntervalMs')}
      />
      <TunableField
        id="kbBudgetTokens"
        label="Budżet tokenów bazy wiedzy"
        type="number"
        description="Miękki budżet tokenów na kontekst z bazy wiedzy. Działa bez restartu."
        rule={{ min: 1 }}
        state={g.kbBudgetTokens}
        saving={save.isPending}
        onSave={onSave('kbBudgetTokens')}
      />
      <TunableField
        id="kbHardCeilingTokens"
        label="Twardy limit tokenów bazy wiedzy"
        type="number"
        description="Górny limit tokenów z bazy wiedzy. Działa bez restartu."
        rule={{ min: 1 }}
        state={g.kbHardCeilingTokens}
        saving={save.isPending}
        onSave={onSave('kbHardCeilingTokens')}
      />
      <TunableField
        id="openaiWhisperModel"
        label="Model transkrypcji (Whisper)"
        description="Model OpenAI do transkrypcji głosówek. Czytany per użycie."
        state={g.openaiWhisperModel}
        saving={save.isPending}
        onSave={onSave('openaiWhisperModel')}
      />
      <TunableField
        id="openaiVisionModel"
        label="Model opisu obrazków (vision)"
        description="Model OpenAI do opisu obrazków. Czytany per użycie."
        state={g.openaiVisionModel}
        saving={save.isPending}
        onSave={onSave('openaiVisionModel')}
      />
    </>
  );
}
