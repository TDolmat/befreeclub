import { cn } from '@/core/lib/utils';
import { AlertTriangle, CheckCircle2, Info, XCircle } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import type * as React from 'react';
import { Link } from 'react-router-dom';

/**
 * Blok komunikatu: ikona + tytuł + opis + opcjonalny CTA. Używany jako inline
 * błąd pod wierszem ustawień (smart-error) i jako ostrzeżenia w sekcjach.
 * CTA: wewnętrzny link (actionRoute) albo dowolny onAction (button).
 */
export type CalloutVariant = 'error' | 'warning' | 'info' | 'success';

const VARIANT_META: Record<CalloutVariant, { icon: LucideIcon; box: string; iconCls: string }> = {
  error: {
    icon: XCircle,
    box: 'border-destructive/40 bg-destructive/10',
    iconCls: 'text-destructive',
  },
  warning: {
    icon: AlertTriangle,
    box: 'border-primary/40 bg-primary/10',
    iconCls: 'text-primary',
  },
  info: { icon: Info, box: 'border-info/40 bg-info/10', iconCls: 'text-info' },
  success: {
    icon: CheckCircle2,
    box: 'border-success/40 bg-success/10',
    iconCls: 'text-success',
  },
};

export interface CalloutProps {
  variant?: CalloutVariant;
  title: string;
  description?: React.ReactNode;
  /** Wewnętrzna trasa (react-router) - renderuje Link. */
  actionRoute?: string;
  /** Etykieta CTA (przy actionRoute albo onAction). */
  actionLabel?: string;
  /** Akcja niebędąca nawigacją (np. ponów test). Ignorowane gdy jest actionRoute. */
  onAction?: () => void;
  className?: string;
}

export function Callout({
  variant = 'info',
  title,
  description,
  actionRoute,
  actionLabel,
  onAction,
  className,
}: CalloutProps) {
  const meta = VARIANT_META[variant];
  const Icon = meta.icon;
  const showCta = !!actionLabel && (!!actionRoute || !!onAction);

  return (
    <div
      className={cn('flex gap-2.5 rounded-lg border px-3 py-2.5 text-[12px]', meta.box, className)}
    >
      <Icon className={cn('h-4 w-4 shrink-0 mt-0.5', meta.iconCls)} />
      <div className="min-w-0 flex-1">
        <div className="font-semibold text-foreground/90">{title}</div>
        {description && <div className="mt-0.5 text-foreground/65">{description}</div>}
        {showCta &&
          (actionRoute ? (
            <Link
              to={actionRoute}
              className="mt-1.5 inline-block font-semibold text-info underline-offset-4 hover:underline"
            >
              {actionLabel}
            </Link>
          ) : (
            <button
              type="button"
              onClick={onAction}
              className="mt-1.5 inline-block font-semibold text-info underline-offset-4 hover:underline"
            >
              {actionLabel}
            </button>
          ))}
      </div>
    </div>
  );
}
