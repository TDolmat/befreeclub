import { AppHeader } from '@/core/components/AppHeader';
import { Callout } from '@/core/components/ui/Callout';
import { useAdminSettings } from '@/core/hooks/useSettings';
import { resolveSmartError } from '@/core/lib/settings-errors';
import { Loader2 } from 'lucide-react';
import { Navigate, Route, Routes } from 'react-router-dom';
import { SettingsCategoryPage } from './settings/SettingsCategoryPage';
import { SettingsNav } from './settings/SettingsNav';
import { SETTINGS_CATEGORIES } from './settings/settings-registry';

const FIRST_CATEGORY = SETTINGS_CATEGORIES[0]?.id ?? 'membership';

/**
 * Shell ustawień (master-detail w stylu Stripe). Trzyma query ustawień i
 * globalne stany loading/error. Lewa kolumna: search + nawigacja kategorii.
 * Prawa: nested routes per kategoria (zły podlink wraca do pierwszej kategorii,
 * nie wyrzuca z ustawień - nginx ma SPA fallback).
 */
export function SettingsPage() {
  const query = useAdminSettings();

  return (
    <div className="min-h-screen bg-background text-foreground flex flex-col">
      <AppHeader />
      <main className="flex-1 mx-auto w-full max-w-[1100px] px-4 sm:px-6 py-8">
        <header className="mb-6">
          <h1 className="text-xl font-semibold text-foreground">Ustawienia</h1>
          <p className="mt-1 text-sm text-foreground/50">
            Kontrolne pokrętła panelu. Ustawienie z panelu nadpisuje wartość z .env serwera.
          </p>
        </header>

        <div className="grid grid-cols-1 gap-8 md:grid-cols-[260px_1fr]">
          <SettingsNav />

          <div className="min-w-0">
            {query.isLoading ? (
              <div className="flex items-center justify-center gap-2 py-16 text-foreground/40">
                <Loader2 className="h-5 w-5 animate-spin" /> Ładuję ustawienia…
              </div>
            ) : query.isError ? (
              <Callout
                variant="error"
                title={resolveSmartError(query.error).title}
                description={resolveSmartError(query.error).hint}
                actionLabel="Spróbuj ponownie"
                onAction={() => query.refetch()}
              />
            ) : query.data ? (
              <Routes>
                <Route index element={<Navigate to={FIRST_CATEGORY} replace />} />
                <Route path=":category" element={<SettingsCategoryPage />} />
                <Route path="*" element={<Navigate to={FIRST_CATEGORY} replace />} />
              </Routes>
            ) : null}
          </div>
        </div>
      </main>
    </div>
  );
}
