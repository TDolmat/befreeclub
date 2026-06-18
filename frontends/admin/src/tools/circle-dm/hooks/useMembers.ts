import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '@/tools/circle-dm/lib/api';

export function useMembers(
  adminAccountId: number | null,
  search: string,
  opts: { excludeWithThread?: boolean } = {},
) {
  const debouncedSearch = useDebounced(search, 250);
  const excludeWithThread = opts.excludeWithThread ?? false;
  return useQuery({
    queryKey: ['members', adminAccountId, debouncedSearch, excludeWithThread],
    queryFn: () =>
      adminAccountId !== null
        ? api.members
            .list({
              adminAccountId,
              q: debouncedSearch || undefined,
              limit: 300,
              excludeWithThread,
            })
            .then((r) => r.members)
        : Promise.resolve([]),
    enabled: adminAccountId !== null,
    staleTime: 60_000,
  });
}

function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const handle = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(handle);
  }, [value, ms]);
  return debounced;
}
