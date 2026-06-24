import { useToast } from '@/core/components/ui/toast';
import { settingsApi } from '@/core/lib/settings-api';
import { resolveSmartError } from '@/core/lib/settings-errors';
import type {
  AnalyticsPatch,
  BillingNewsletterPatch,
  CircleDmAiPatch,
  MembershipPatch,
} from '@bfc/shared';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

const SETTINGS_KEY = ['admin', 'settings'] as const;
const CONNECTIONS_KEY = ['admin', 'connections'] as const;

export function useAdminSettings() {
  return useQuery({
    queryKey: SETTINGS_KEY,
    queryFn: () => settingsApi.get(),
  });
}

type GroupPatch = {
  circleDmAi: CircleDmAiPatch;
  membership: MembershipPatch;
  billingNewsletter: BillingNewsletterPatch;
  analytics: AnalyticsPatch;
};

/** Zapis patcha jednej grupy. Po sukcesie refetch + toast. */
export function useSaveSettingsGroup<G extends keyof GroupPatch>(group: G) {
  const qc = useQueryClient();
  const { toast } = useToast();
  return useMutation({
    mutationFn: (patch: GroupPatch[G]) => settingsApi.update(group, patch),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: SETTINGS_KEY });
      toast({ kind: 'success', title: 'Zapisane' });
    },
    onError: (err) => {
      const e = resolveSmartError(err);
      toast({ kind: 'error', title: e.title, description: e.hint });
    },
  });
}

export function useConnections() {
  return useQuery({
    queryKey: CONNECTIONS_KEY,
    // Tani listing - sam status z env. Test-call odpalamy ręcznie per API.
    queryFn: () => settingsApi.connections(false),
  });
}

export function useTestConnection() {
  const qc = useQueryClient();
  const { toast } = useToast();
  return useMutation({
    mutationFn: (key: string) => settingsApi.testConnection(key),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: CONNECTIONS_KEY });
      const c = res.connection;
      toast({
        kind: c.status === 'ok' ? 'success' : c.status === 'error' ? 'error' : 'info',
        title: `${c.label}: ${c.status}`,
        description: c.detail,
      });
    },
    // Sukces (HTTP 200 ze status) idzie wyzej bez zmian - ConnectionsSection
    // uzywa resolveConnectionError. Tu lapiemy tylko realny rzut (sesja/sieć).
    onError: (err) => {
      const e = resolveSmartError(err);
      toast({ kind: 'error', title: e.title, description: e.hint });
    },
  });
}

/**
 * Ustawienie klucza API (jeden z 4 edytowalnych). Po sukcesie odśwież listę
 * połączeń + toast. Wartość leci tylko w mutationFn, nie zostaje w cache.
 */
export function useSetConnectionSecret() {
  const qc = useQueryClient();
  const { toast } = useToast();
  return useMutation({
    mutationFn: ({ key, value }: { key: string; value: string }) =>
      settingsApi.setConnectionSecret(key, value),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: CONNECTIONS_KEY });
      toast({ kind: 'success', title: 'Klucz zapisany' });
    },
    onError: (err) => {
      const e = resolveSmartError(err);
      toast({ kind: 'error', title: e.title, description: e.hint });
    },
  });
}

/** Wyczyszczenie klucza z panelu - powrót do wartości z env. */
export function useClearConnectionSecret() {
  const qc = useQueryClient();
  const { toast } = useToast();
  return useMutation({
    mutationFn: (key: string) => settingsApi.clearConnectionSecret(key),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: CONNECTIONS_KEY });
      toast({ kind: 'success', title: 'Wrócono do env' });
    },
    onError: (err) => {
      const e = resolveSmartError(err);
      toast({ kind: 'error', title: e.title, description: e.hint });
    },
  });
}

export function useRunCleanup() {
  const qc = useQueryClient();
  const { toast } = useToast();
  return useMutation({
    mutationFn: () => settingsApi.runCleanup(),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: SETTINGS_KEY });
      toast({
        kind: 'success',
        title: res.dryRun ? 'Tryb cienia: policzone' : 'Cleanup wykonany',
        description: res.dryRun
          ? `Sprawdzono ${res.checked}, do usunięcia ${res.wouldRemove}. Nikt nie ruszony.`
          : `Sprawdzono ${res.checked}, usunięto ${res.removed}.`,
      });
    },
    onError: (err) => {
      const e = resolveSmartError(err);
      toast({ kind: 'error', title: e.title, description: e.hint });
    },
  });
}
