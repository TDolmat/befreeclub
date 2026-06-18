import * as React from 'react';
import { CheckCircle2, XCircle, AlertTriangle, Info } from 'lucide-react';
import { cn } from '@/core/lib/utils';

type ToastKind = 'success' | 'error' | 'warning' | 'info';

interface Toast {
  id: number;
  kind: ToastKind;
  title: string;
  description?: string;
}

interface ToastContextValue {
  toast: (input: Omit<Toast, 'id'>) => void;
}

const ToastContext = React.createContext<ToastContextValue | null>(null);

export function useToast(): ToastContextValue {
  const ctx = React.useContext(ToastContext);
  if (!ctx) throw new Error('useToast must be used inside <ToastProvider>');
  return ctx;
}

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = React.useState<Toast[]>([]);
  const counterRef = React.useRef(0);

  const toast = React.useCallback((input: Omit<Toast, 'id'>) => {
    const id = ++counterRef.current;
    setToasts((current) => [...current, { id, ...input }]);
    setTimeout(() => {
      setToasts((current) => current.filter((t) => t.id !== id));
    }, 4500);
  }, []);

  return (
    <ToastContext.Provider value={{ toast }}>
      {children}
      <div className="fixed bottom-4 right-4 z-[200] flex flex-col gap-2 max-w-sm">
        {toasts.map((t) => (
          <ToastItem key={t.id} toast={t} />
        ))}
      </div>
    </ToastContext.Provider>
  );
}

function ToastItem({ toast }: { toast: Toast }) {
  const Icon =
    toast.kind === 'success'
      ? CheckCircle2
      : toast.kind === 'error'
        ? XCircle
        : toast.kind === 'warning'
          ? AlertTriangle
          : Info;
  const ring =
    toast.kind === 'success'
      ? 'border-success/30 bg-card'
      : toast.kind === 'error'
        ? 'border-destructive/30 bg-card'
        : toast.kind === 'warning'
          ? 'border-primary/30 bg-card'
          : 'border-info/30 bg-card';
  const iconColor =
    toast.kind === 'success'
      ? 'text-success'
      : toast.kind === 'error'
        ? 'text-destructive'
        : toast.kind === 'warning'
          ? 'text-primary'
          : 'text-info';
  return (
    <div
      className={cn(
        'flex gap-3 rounded-lg border p-3 shadow-glow-card animate-fade-in-up',
        ring,
      )}
    >
      <Icon className={cn('h-5 w-5 shrink-0 mt-0.5', iconColor)} />
      <div className="flex-1 min-w-0">
        <div className="text-sm font-semibold text-foreground">{toast.title}</div>
        {toast.description && (
          <div className="text-xs text-foreground/70 mt-0.5 break-words">{toast.description}</div>
        )}
      </div>
    </div>
  );
}
