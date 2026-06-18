import { CheckCheck } from 'lucide-react';
import { Link } from 'react-router-dom';
import type { CommunityMember } from '@bfc/shared';
import { Avatar } from '@/core/components/Avatar';
import { cn } from '@/core/lib/utils';

interface Props {
  member: CommunityMember;
  selectable?: {
    isSelected: boolean;
    onToggle: () => void;
  };
  /**
   * When true, clicking anywhere on the card toggles selection instead of
   * opening compose. Matches the thread cards' select-mode UX so the whole
   * inbox is consistent: once anything is selected, every other click adds
   * to the selection.
   */
  selectionMode?: boolean;
}

export function MemberCard({ member, selectable, selectionMode }: Props) {
  const inner = (
    <div
      className={cn(
        'rounded-lg border bg-card p-3 flex items-center gap-3 h-full transition-colors',
        selectable?.isSelected
          ? 'border-primary/40 bg-primary/5'
          : 'border-border hover:bg-card-hover hover:border-primary/30',
      )}
    >
      {selectable && (
        <button
          type="button"
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            selectable.onToggle();
          }}
          className={cn(
            'shrink-0 h-5 w-5 rounded border-2 grid place-items-center transition-colors',
            selectable.isSelected
              ? 'border-primary bg-primary/20'
              : 'border-border hover:border-primary/60',
          )}
          title={selectable.isSelected ? 'Odznacz' : 'Zaznacz'}
        >
          {selectable.isSelected && <CheckCheck className="h-3 w-3 text-primary" />}
        </button>
      )}

      <Avatar name={member.name} url={member.avatarUrl} size="md" />
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-1.5">
          <div className="font-semibold text-sm truncate text-foreground">{member.name}</div>
          {member.isAdmin && (
            <span className="badge-brand text-[9px] uppercase tracking-wider px-1.5 py-0 rounded">
              admin
            </span>
          )}
        </div>
        {member.headline && (
          <div className="text-xs text-foreground/60 truncate">{member.headline}</div>
        )}
        {member.lastSeenText && !member.headline && (
          <div className="text-xs text-foreground/40 truncate">{member.lastSeenText}</div>
        )}
      </div>
    </div>
  );

  if (selectionMode && selectable) {
    return (
      <button
        type="button"
        onClick={selectable.onToggle}
        className="group block w-full text-left"
        title="Tryb zaznaczania - klik zaznacza/odznacza"
      >
        {inner}
      </button>
    );
  }

  return (
    <Link
      to={`/circle-dm/compose?member=${member.id}`}
      className="group block"
      title={`Napisz do ${member.name}`}
    >
      {inner}
    </Link>
  );
}
