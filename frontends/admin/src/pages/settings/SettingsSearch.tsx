import { Badge } from '@/core/components/ui/Badge';
import { Input } from '@/core/components/ui/input';
import { cn, foldText } from '@/core/lib/utils';
import { Search } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { SETTINGS_SEARCH_INDEX, type SearchIndexEntry } from './settings-registry';

/**
 * Globalny search ustawień nad nawigacją kategorii. Matchuje label + opis +
 * keywords (z tolerancją polskich znaków przez foldText). Klik/Enter prowadzi
 * do kategorii pola z ?focus=fieldId (deep-link scroll + highlight).
 * Własny dropdown, bez nowej zależności (cmdk nie ma w deps).
 */
export function SettingsSearch() {
  const navigate = useNavigate();
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);

  const results = useMemo<SearchIndexEntry[]>(() => {
    const folded = foldText(query);
    if (!folded) return [];
    return SETTINGS_SEARCH_INDEX.filter((entry) => {
      const haystack = [
        entry.label,
        entry.description,
        entry.categoryLabel,
        ...entry.keywords,
      ].join(' ');
      return foldText(haystack).includes(folded);
    }).slice(0, 8);
  }, [query]);

  // Klamruj zaznaczenie do długości wyników (po zmianie query lista maleje).
  const activeIndex = results.length > 0 ? Math.min(active, results.length - 1) : 0;

  // Klik poza komponentem zamyka dropdown.
  useEffect(() => {
    if (!open) return;
    function onClick(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener('mousedown', onClick);
    return () => document.removeEventListener('mousedown', onClick);
  }, [open]);

  function go(entry: SearchIndexEntry) {
    navigate(`${entry.route}?focus=${entry.fieldId}`);
    setQuery('');
    setOpen(false);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Escape') {
      setOpen(false);
      return;
    }
    if (!open || results.length === 0) {
      if (e.key === 'ArrowDown' && query) setOpen(true);
      return;
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActive((i) => (Math.min(i, results.length - 1) + 1) % results.length);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((i) => (Math.min(i, results.length - 1) - 1 + results.length) % results.length);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const entry = results[activeIndex];
      if (entry) go(entry);
    }
  }

  const showDropdown = open && query.length > 0;

  return (
    <div ref={rootRef} className="relative">
      <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-foreground/40" />
      <Input
        value={query}
        onChange={(e) => {
          setQuery(e.target.value);
          setActive(0);
          setOpen(true);
        }}
        onFocus={() => query && setOpen(true)}
        onKeyDown={onKeyDown}
        placeholder="Szukaj ustawienia…"
        className="bg-background border-foreground/20 pl-9"
        aria-label="Szukaj ustawień"
        role="combobox"
        aria-expanded={showDropdown}
        aria-controls="settings-search-list"
        aria-autocomplete="list"
        aria-activedescendant={
          showDropdown && results.length > 0 ? `settings-opt-${activeIndex}` : undefined
        }
      />

      {showDropdown && (
        <div className="absolute left-0 right-0 top-full z-50 mt-1 max-h-80 overflow-y-auto rounded-lg border border-foreground/15 bg-card shadow-xl">
          {results.length === 0 ? (
            <p className="px-3 py-3 text-[12px] text-foreground/55" aria-live="polite">
              Nic nie pasuje do „{query}”.
            </p>
          ) : (
            <>
              <p className="sr-only" aria-live="polite">
                {results.length === 1 ? '1 wynik' : `${results.length} wyników`}
              </p>
              {/* Wzorzec WAI-ARIA combobox: listbox/option na ul/li jest poprawny,
                  fokus trzyma input przez aria-activedescendant. Biome tego nie rozróżnia. */}
              {/* biome-ignore lint/a11y/useFocusableInteractive: fokus na inpucie przez aria-activedescendant */}
              <ul
                id="settings-search-list"
                // biome-ignore lint/a11y/noNoninteractiveElementToInteractiveRole: wzorzec ARIA combobox
                // biome-ignore lint/a11y/useSemanticElements: brak natywnego odpowiednika dla listbox
                role="listbox"
                aria-label="Wyniki wyszukiwania ustawień"
              >
                {results.map((entry, i) => (
                  // biome-ignore lint/a11y/useFocusableInteractive: fokus na inpucie przez aria-activedescendant
                  <li
                    key={`${entry.categoryId}-${entry.fieldId}`}
                    id={`settings-opt-${i}`}
                    // biome-ignore lint/a11y/noNoninteractiveElementToInteractiveRole: wzorzec ARIA combobox
                    // biome-ignore lint/a11y/useSemanticElements: brak natywnego odpowiednika dla option
                    role="option"
                    aria-selected={i === activeIndex}
                  >
                    <button
                      type="button"
                      onMouseEnter={() => setActive(i)}
                      onClick={() => go(entry)}
                      className={cn(
                        'flex w-full items-center justify-between gap-3 px-3 py-2.5 text-left',
                        i === activeIndex ? 'bg-primary/15' : 'hover:bg-foreground/5',
                      )}
                    >
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm text-foreground">
                          {entry.label}
                        </span>
                        <span className="block truncate text-[11px] text-foreground/50">
                          {entry.description}
                        </span>
                      </span>
                      <Badge variant="muted" className="shrink-0">
                        {entry.categoryLabel}
                      </Badge>
                    </button>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </div>
  );
}
