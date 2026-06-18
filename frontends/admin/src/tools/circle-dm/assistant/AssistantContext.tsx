import {
  type ReactNode,
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import type { AssistantContext as AssistantContextSnapshot } from '@bfc/shared';

const STORAGE_KEY = 'bfc-admin:assistant-open';

interface ContextValue {
  isOpen: boolean;
  open: () => void;
  close: () => void;
  toggle: () => void;
  /** Latest snapshot from whichever page registered last. */
  snapshot: AssistantContextSnapshot;
}

const Ctx = createContext<ContextValue | null>(null);

/**
 * Internal: latest snapshot held in a ref so pages can update it without
 * triggering a re-render of the whole provider tree. The panel reads it at
 * Send time via getSnapshot() exposed through context.
 */
interface InternalRef {
  current: AssistantContextSnapshot;
  setter: (v: AssistantContextSnapshot) => void;
}

const internalRefCtx = createContext<InternalRef | null>(null);

export function AssistantProvider({ children }: { children: ReactNode }) {
  const [isOpen, setIsOpen] = useState<boolean>(() => {
    try {
      return localStorage.getItem(STORAGE_KEY) === '1';
    } catch {
      return false;
    }
  });
  const [snapshot, setSnapshot] = useState<AssistantContextSnapshot>({ kind: 'none' });

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, isOpen ? '1' : '0');
    } catch {
      /* private mode: ignore */
    }
  }, [isOpen]);

  const internalRef = useRef<InternalRef>({
    current: { kind: 'none' },
    setter: setSnapshot,
  });
  // Keep the ref's setter in sync (setSnapshot identity is stable, this is
  // just defensive).
  internalRef.current.setter = setSnapshot;
  internalRef.current.current = snapshot;

  const value = useMemo<ContextValue>(
    () => ({
      isOpen,
      open: () => setIsOpen(true),
      close: () => setIsOpen(false),
      toggle: () => setIsOpen((v) => !v),
      snapshot,
    }),
    [isOpen, snapshot],
  );

  return (
    <Ctx.Provider value={value}>
      <internalRefCtx.Provider value={internalRef.current}>{children}</internalRefCtx.Provider>
    </Ctx.Provider>
  );
}

export function useAssistant(): ContextValue {
  const v = useContext(Ctx);
  if (!v) throw new Error('useAssistant outside AssistantProvider');
  return v;
}

/**
 * Pages call this with their current snapshot. Latest mounted page wins; on
 * unmount the snapshot resets to {kind:'none'} so the assistant doesn't
 * carry stale view context across routes.
 *
 * Snapshot equality is JSON-based so re-renders with same data don't churn.
 */
export function useRegisterAssistantContext(snapshot: AssistantContextSnapshot): void {
  const ref = useContext(internalRefCtx);
  const lastJsonRef = useRef<string>('');
  // Serialise once per snapshot identity to compare cheaply.
  const json = useMemo(() => JSON.stringify(snapshot), [snapshot]);

  // Push on change.
  useEffect(() => {
    if (!ref) return;
    if (json === lastJsonRef.current) return;
    lastJsonRef.current = json;
    ref.setter(snapshot);
    // snapshot used via json identity; intentionally exclude from deps
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [json, ref]);

  // Reset on unmount.
  useEffect(() => {
    if (!ref) return;
    return () => {
      ref.setter({ kind: 'none' });
    };
  }, [ref]);
}

/** Toggle via Cmd/Ctrl+J anywhere except inside text inputs/textareas. */
export function useAssistantShortcut(): void {
  const { toggle } = useAssistant();
  const toggleRef = useRef(toggle);
  toggleRef.current = toggle;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const isCombo = (e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'j';
      if (!isCombo) return;
      e.preventDefault();
      toggleRef.current();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
}

