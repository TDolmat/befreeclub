import { LayoutGrid } from 'lucide-react';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { FeedbackButton } from '@/core/feedback/FeedbackButton';

export function AppHeader({
  subNav,
  rightExtras,
}: {
  subNav?: ReactNode;
  rightExtras?: ReactNode;
}) {
  return (
    <header className="sticky top-0 z-40 border-b border-border bg-card/80 backdrop-blur-xl">
      <div className="mx-auto flex h-16 max-w-[1400px] items-center justify-between px-4 sm:px-6 gap-3">
        <Link to="/" className="flex items-center gap-3 hover:opacity-90 transition-opacity">
          <div className="h-9 w-9 rounded-lg bg-primary/15 border border-primary/30 grid place-items-center">
            <LayoutGrid className="h-5 w-5 text-primary" />
          </div>
          <div className="flex flex-col leading-tight">
            <span className="auth-title text-base sm:text-lg">Be Free Club</span>
            <span className="text-[10px] uppercase tracking-widest text-foreground/40">
              Panel administratora
            </span>
          </div>
        </Link>

        <div className="flex items-center gap-2">
          {subNav && <nav className="flex items-center gap-2">{subNav}</nav>}
          <FeedbackButton />
          {rightExtras}
        </div>
      </div>
    </header>
  );
}
