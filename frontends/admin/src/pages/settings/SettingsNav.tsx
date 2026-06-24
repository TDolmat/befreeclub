import { cn } from '@/core/lib/utils';
import { NavLink } from 'react-router-dom';
import { SettingsSearch } from './SettingsSearch';
import { SETTINGS_CATEGORIES } from './settings-registry';

/**
 * Lewa kolumna master-detail: search na górze + NavLink per kategoria z
 * registry. Sticky, bez glow. Aktywny element ma akcent primary z lewą belką.
 */
export function SettingsNav() {
  return (
    <aside className="sticky top-20 self-start">
      <SettingsSearch />
      <nav className="mt-4 flex flex-col gap-1">
        {SETTINGS_CATEGORIES.map((cat) => {
          const Icon = cat.icon;
          return (
            <NavLink
              key={cat.id}
              to={cat.route}
              className={({ isActive }) =>
                cn(
                  'flex items-center gap-2.5 rounded-md border-l-2 px-3 py-2 text-sm transition-colors',
                  isActive
                    ? 'border-primary bg-primary/15 text-primary font-semibold'
                    : 'border-transparent text-foreground/70 hover:bg-foreground/5',
                )
              }
            >
              <Icon className="h-4 w-4 shrink-0" />
              <span className="truncate">{cat.label}</span>
            </NavLink>
          );
        })}
      </nav>
    </aside>
  );
}
