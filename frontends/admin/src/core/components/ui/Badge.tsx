import { cn } from '@/core/lib/utils';
import type * as React from 'react';

/**
 * Mała plakietka statusu/etykiety. Owija klasy .badge-* z index.css (brand,
 * success, error, info), dokłada warning (żółty BFC) i muted (neutralny).
 * Używana w SettingsRow (badge restart/env) i w statusach połączeń.
 */
export type BadgeVariant = 'brand' | 'success' | 'error' | 'info' | 'warning' | 'muted';

const VARIANT_CLS: Record<BadgeVariant, string> = {
  brand: 'badge-brand',
  success: 'badge-success',
  error: 'badge-error',
  info: 'badge-info',
  // warning: brak klasy .badge-warning w index.css - składamy z tokenu primary (żółty BFC).
  warning: 'bg-primary/15 text-primary border border-primary/30',
  // muted: neutralny, czytelny na ciemnym tle (nie używa --muted == --card).
  muted: 'bg-foreground/10 text-foreground/60 border border-foreground/15',
};

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  variant?: BadgeVariant;
}

export function Badge({ variant = 'muted', className, ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide leading-none',
        VARIANT_CLS[variant],
        className,
      )}
      {...props}
    />
  );
}
