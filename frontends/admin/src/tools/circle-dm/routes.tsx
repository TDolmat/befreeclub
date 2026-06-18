import { Navigate, Route, Routes } from 'react-router-dom';
import { AppHeader } from '@/core/components/AppHeader';
import { cn } from '@/core/lib/utils';
import { DmSubNav } from './components/DmSubNav';
import { InboxPage } from './pages/InboxPage';
import { ThreadPage } from './pages/ThreadPage';
import { AccountsPage } from './pages/AccountsPage';
import { ComposePage } from './pages/ComposePage';
import { BulkComposePage } from './pages/BulkComposePage';
import { SettingsPage } from './pages/SettingsPage';
import {
  AssistantProvider,
  useAssistant,
  useAssistantShortcut,
} from './assistant/AssistantContext';
import { AssistantPanel } from './assistant/AssistantPanel';
import { AssistantToggle } from './assistant/AssistantToggle';

export function DmRoutes() {
  return (
    <AssistantProvider>
      <DmShell />
    </AssistantProvider>
  );
}

function DmShell() {
  const { isOpen } = useAssistant();
  useAssistantShortcut();
  return (
    <div className="min-h-screen bg-background text-foreground flex flex-col">
      <AppHeader subNav={<DmSubNav />} rightExtras={<AssistantToggle />} />
      <main
        className={cn(
          'flex-1 mx-auto w-full max-w-[1400px] px-4 sm:px-6 py-6 transition-[padding] duration-200',
          isOpen && 'sm:pr-[416px]',
        )}
      >
        <Routes>
          <Route index element={<InboxPage />} />
          <Route path="thread/:id" element={<ThreadPage />} />
          <Route path="accounts" element={<AccountsPage />} />
          <Route path="compose" element={<ComposePage />} />
          <Route path="bulk-compose" element={<BulkComposePage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/circle-dm" replace />} />
        </Routes>
      </main>
      <AssistantPanel />
    </div>
  );
}
