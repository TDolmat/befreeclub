import { Callout } from '@/core/components/ui/Callout';
import type { CalloutProps } from '@/core/components/ui/Callout';
import { cn } from '@/core/lib/utils';
import type * as React from 'react';

/**
 * Generyczny wiersz ustawień w stylu Stripe: po lewej label + opis, po prawej
 * kontrolka (children). id == fieldId = cel deep-linku (?focus=fieldId scroll +
 * highlight). Błąd renderowany jako inline Callout pod wierszem.
 *
 * Wygląd kontrolek (Switch/Input) wzmacniają same sekcje przez className
 * (DECYZJA #2) - ten wiersz daje tylko czytelne tło/ramkę i layout.
 */
export interface SettingsRowProps {
  /** camelCase klucz pola z @bfc/shared. DOM id wrappera (deep-link). */
  fieldId?: string;
  label: React.ReactNode;
  description?: React.ReactNode;
  /** Plakietka po prawej obok labela (np. restart/env). */
  badge?: React.ReactNode;
  /** Kontrolka po prawej stronie wiersza. */
  children?: React.ReactNode;
  /** Inline błąd pod wierszem (smart-error). */
  error?: CalloutProps | null;
  /** Wiersz układa kontrolkę pod opisem zamiast obok (szersze kontrolki). */
  stacked?: boolean;
  className?: string;
}

export function SettingsRow({
  fieldId,
  label,
  description,
  badge,
  children,
  error,
  stacked = false,
  className,
}: SettingsRowProps) {
  return (
    <div
      id={fieldId}
      className={cn(
        'scroll-mt-24 rounded-lg border border-foreground/10 bg-foreground/[0.03] px-4 py-3.5 transition-shadow',
        className,
      )}
    >
      <div className={cn('gap-4', stacked ? 'flex flex-col' : 'flex items-start justify-between')}>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-semibold text-foreground">{label}</span>
            {badge}
          </div>
          {description && (
            <p className="mt-1 text-[12px] leading-snug text-foreground/55">{description}</p>
          )}
        </div>
        {children != null && <div className={cn('shrink-0', stacked && 'w-full')}>{children}</div>}
      </div>
      {error && <Callout {...error} className="mt-3" />}
    </div>
  );
}
