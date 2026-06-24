import { useAdminSettings, useSaveSettingsGroup } from '@/core/hooks/useSettings';
import type { AnalyticsGroup, BillingNewsletterGroup } from '@bfc/shared';
import { TunableField } from './TunableField';
import { getCategory } from './settings-registry';

/**
 * Sekcja Billing & Newsletter: niesekretne parametry mailowe i newslettera.
 * Self-contained (własny useAdminSettings), bez propsów. Nagłówek kategorii
 * rysuje SettingsCategoryPage z registry - tu tylko lista wierszy SettingsRow
 * (przez TunableField). Klucze API mieszkają w Połączeniach, nie tu.
 *
 * Labelki/opisy biorę z registry (jedno źródło prawdy), rule odzwierciedla
 * walidację backendu (url:true dla adresów).
 */
const FIELDS = getCategory('billingNewsletter')?.fields ?? [];
function meta(fieldId: string) {
  return FIELDS.find((f) => f.fieldId === fieldId);
}

export function BillingNewsletterSection() {
  const query = useAdminSettings();
  const save = useSaveSettingsGroup('billingNewsletter');
  const data = query.data?.groups.billingNewsletter;
  const onSave = (key: keyof BillingNewsletterGroup) => (value: string | number | null) =>
    save.mutate({ [key]: { value } });

  if (!data) return null;

  return (
    <>
      <TunableField
        id="frontendUrl"
        label={meta('frontendUrl')?.label ?? 'Adres frontu (linki w mailach)'}
        description={meta('frontendUrl')?.description}
        state={data.frontendUrl}
        saving={save.isPending}
        onSave={onSave('frontendUrl')}
        rule={{ url: true }}
      />
      <TunableField
        id="confirmUrlBase"
        label={meta('confirmUrlBase')?.label ?? 'Adres potwierdzenia newslettera'}
        description={meta('confirmUrlBase')?.description}
        state={data.confirmUrlBase}
        saving={save.isPending}
        onSave={onSave('confirmUrlBase')}
        rule={{ url: true }}
      />
      <TunableField
        id="cancellationFromEmail"
        label={meta('cancellationFromEmail')?.label ?? 'Nadawca maili (anulowanie/karta)'}
        description={meta('cancellationFromEmail')?.description}
        state={data.cancellationFromEmail}
        saving={save.isPending}
        onSave={onSave('cancellationFromEmail')}
      />
      <TunableField
        id="newsletterFromEmail"
        label={meta('newsletterFromEmail')?.label ?? 'Nadawca maili newslettera'}
        description={meta('newsletterFromEmail')?.description}
        state={data.newsletterFromEmail}
        saving={save.isPending}
        onSave={onSave('newsletterFromEmail')}
      />
      <TunableField
        id="senderGroupIds"
        label={meta('senderGroupIds')?.label ?? 'Grupy Sender.net (id, po przecinku)'}
        description={meta('senderGroupIds')?.description}
        state={data.senderGroupIds}
        saving={save.isPending}
        onSave={onSave('senderGroupIds')}
      />
      <TunableField
        id="ebookFilePath"
        label={meta('ebookFilePath')?.label ?? 'Ścieżka pliku ebooka (na dysku VPS)'}
        description={meta('ebookFilePath')?.description}
        state={data.ebookFilePath}
        saving={save.isPending}
        onSave={onSave('ebookFilePath')}
      />
    </>
  );
}

/**
 * Sekcja Analityka: niesekretne parametry pomiarowe. circleCommunityId zostaje
 * tutaj do EDYCJI (DECYZJA #4) - w Połączeniach pokazujemy tylko status Circle.
 * Token Meta CAPI jest w Połączeniach. Self-contained, bez propsów.
 */
const ANALYTICS_FIELDS = getCategory('analytics')?.fields ?? [];
function analyticsMeta(fieldId: string) {
  return ANALYTICS_FIELDS.find((f) => f.fieldId === fieldId);
}

export function AnalyticsSection() {
  const query = useAdminSettings();
  const save = useSaveSettingsGroup('analytics');
  const data = query.data?.groups.analytics;
  const onSave = (key: keyof AnalyticsGroup) => (value: string | number | null) =>
    save.mutate({ [key]: { value } });

  if (!data) return null;

  return (
    <>
      <TunableField
        id="metaPixelId"
        label={analyticsMeta('metaPixelId')?.label ?? 'Meta Pixel ID'}
        description={analyticsMeta('metaPixelId')?.description}
        state={data.metaPixelId}
        saving={save.isPending}
        onSave={onSave('metaPixelId')}
      />
      <TunableField
        id="circleCommunityId"
        label={analyticsMeta('circleCommunityId')?.label ?? 'Circle community ID'}
        description={analyticsMeta('circleCommunityId')?.description}
        state={data.circleCommunityId}
        saving={save.isPending}
        onSave={onSave('circleCommunityId')}
      />
    </>
  );
}
