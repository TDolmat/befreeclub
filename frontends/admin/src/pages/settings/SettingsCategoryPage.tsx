import { Callout } from '@/core/components/ui/Callout';
import { useEffect, useRef } from 'react';
import { useParams, useSearchParams } from 'react-router-dom';
import { AnalyticsSection, BillingNewsletterSection } from './BillingNewsletterSection';
import { CircleDmAiSection } from './CircleDmAiSection';
import { ConnectionsSection } from './ConnectionsSection';
import { MembershipSection } from './MembershipSection';
import { type SettingsCategoryId, getCategory } from './settings-registry';

/**
 * Detal master-detail: z :category bierze meta z registry (nagłówek tu, nie w
 * sekcji), renderuje właściwy komponent sekcji. ?focus=fieldId scrolluje do
 * #fieldId i podświetla wiersz na ~1.5s (deep-link z searcha).
 *
 * Sekcje są self-contained (same wołają useAdminSettings()), więc ten plik nie
 * wątkuje już danych - tylko mapuje kategorię na komponent.
 */
export function SettingsCategoryPage() {
  const { category } = useParams<{ category: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const focus = searchParams.get('focus');

  const meta = category ? getCategory(category) : undefined;

  // Aktywne podświetlenie (element + timer zdejmujący). Trzymane w ref, NIE w
  // cleanupie efektu: czyszczenie ?focus zmienia `focus` (dependency), więc
  // cleanup efektu odpala się tuż po podświetleniu. Gdyby to on kasował timer,
  // ring zostawałby na zawsze, a każdy kolejny search dokładał następny.
  const activeHighlight = useRef<{ el: HTMLElement; timer: number } | null>(null);

  // Deep-link: scroll + chwilowy highlight (~1.5s). Element może pojawić się
  // asynchronicznie (Połączenia ładują status lazy), więc czekamy na #fieldId
  // przez requestAnimationFrame z limitem klatek. Nowe podświetlenie najpierw
  // gasi poprzednie - bez kumulacji ringów przy kolejnych wyszukaniach.
  useEffect(() => {
    if (!focus) return;

    let rafId: number | undefined;
    let frames = 0;
    const MAX_FRAMES = 30; // ~500ms przy 60fps - tyle dajemy lazy sekcji na render

    const clearFocusParam = () => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.delete('focus');
          return next;
        },
        { replace: true },
      );
    };

    const clearActiveHighlight = () => {
      const prev = activeHighlight.current;
      if (prev) {
        prev.el.classList.remove('ring-2', 'ring-primary');
        window.clearTimeout(prev.timer);
        activeHighlight.current = null;
      }
    };

    const highlight = (el: HTMLElement) => {
      clearActiveHighlight(); // zgaś poprzedni ring zanim zapalisz nowy
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      el.classList.add('ring-2', 'ring-primary');
      const timer = window.setTimeout(() => {
        el.classList.remove('ring-2', 'ring-primary');
        activeHighlight.current = null;
      }, 1500);
      activeHighlight.current = { el, timer };
    };

    const tick = () => {
      const el = document.getElementById(focus);
      if (el) {
        highlight(el);
        clearFocusParam();
        return;
      }
      frames += 1;
      if (frames >= MAX_FRAMES) {
        // Element nie pojawił się w limicie - i tak czyścimy param, żeby nie
        // zostawić brudnego URL-a (?focus) wiszącego bez efektu.
        clearFocusParam();
        return;
      }
      rafId = window.requestAnimationFrame(tick);
    };

    rafId = window.requestAnimationFrame(tick);

    // Cleanup anuluje TYLKO polling rAF. Timer zdejmujący ring zostaje (gasi go
    // następne podświetlenie albo jego własny setTimeout), żeby zmiana ?focus
    // nie zostawiała ringu na stałe.
    return () => {
      if (rafId !== undefined) window.cancelAnimationFrame(rafId);
    };
  }, [focus, setSearchParams]);

  // Sprzątanie przy odmontowaniu strony (zmiana kategorii): zgaś wiszący ring.
  useEffect(() => {
    return () => {
      const prev = activeHighlight.current;
      if (prev) {
        prev.el.classList.remove('ring-2', 'ring-primary');
        window.clearTimeout(prev.timer);
        activeHighlight.current = null;
      }
    };
  }, []);

  if (!meta) {
    return (
      <Callout
        variant="warning"
        title="Nie znam tej kategorii ustawień"
        description="Wybierz kategorię z listy po lewej."
      />
    );
  }

  return (
    <section>
      <header className="mb-6">
        <h2 className="text-lg font-semibold text-foreground">{meta.label}</h2>
        <p className="mt-1 max-w-2xl text-sm text-foreground/55">{meta.description}</p>
      </header>
      <div className="flex flex-col gap-3">{renderSection(meta.id)}</div>
    </section>
  );
}

function renderSection(id: SettingsCategoryId) {
  switch (id) {
    case 'membership':
      return <MembershipSection />;
    case 'circleDmAi':
      return <CircleDmAiSection />;
    case 'billingNewsletter':
      return <BillingNewsletterSection />;
    case 'analytics':
      return <AnalyticsSection />;
    case 'connections':
      return <ConnectionsSection />;
  }
}
