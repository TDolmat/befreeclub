import { useState } from 'react';
import { cn } from '@/core/lib/utils';
import { getInitials } from '@/core/lib/format';

interface AvatarProps {
  name: string | null | undefined;
  url?: string | null;
  size?: 'sm' | 'md' | 'lg';
  className?: string;
}

export function Avatar({ name, url, size = 'md', className }: AvatarProps) {
  const [failed, setFailed] = useState(false);
  const sizeClass = size === 'sm' ? 'h-8 w-8 text-xs' : size === 'lg' ? 'h-14 w-14 text-lg' : 'h-10 w-10 text-sm';

  if (url && !failed) {
    return (
      <img
        src={url}
        alt={name ?? ''}
        onError={() => setFailed(true)}
        className={cn('rounded-full object-cover border border-border', sizeClass, className)}
      />
    );
  }

  return (
    <div
      className={cn(
        'rounded-full grid place-items-center font-bold bg-primary/15 text-primary border border-primary/30',
        sizeClass,
        className,
      )}
    >
      {getInitials(name)}
    </div>
  );
}
