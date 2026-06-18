import { useEffect, useSyncExternalStore } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '@/tools/circle-dm/lib/api';
import {
  getActiveAccountId,
  onActiveAccountChange,
  setActiveAccountId,
} from '@/tools/circle-dm/lib/account-context';

export function useAccounts() {
  return useQuery({
    queryKey: ['accounts'],
    queryFn: () => api.accounts.list().then((r) => r.accounts),
  });
}

export function useActiveAccountId(): number | null {
  return useSyncExternalStore(
    (cb) => onActiveAccountChange(cb),
    getActiveAccountId,
    () => null,
  );
}

/**
 * Auto-select first active account if user hasn't picked one yet.
 */
export function useEnsureActiveAccount() {
  const activeId = useActiveAccountId();
  const { data: accounts } = useAccounts();

  useEffect(() => {
    if (activeId === null && accounts && accounts.length > 0) {
      const firstActive = accounts.find((a) => a.isActive) ?? accounts[0];
      if (firstActive) setActiveAccountId(firstActive.id);
    }
    if (activeId !== null && accounts && !accounts.find((a) => a.id === activeId)) {
      setActiveAccountId(null);
    }
  }, [activeId, accounts]);

  return activeId;
}
