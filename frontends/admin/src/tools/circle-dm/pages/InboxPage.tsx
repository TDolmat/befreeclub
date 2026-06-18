import { type ReactNode, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { CommunityMember } from '@bfc/shared';
import {
  Archive,
  ArrowRight,
  CheckCheck,
  ChevronDown,
  Clock3,
  Flag,
  FlagOff,
  FolderInput,
  Inbox,
  Info,
  Loader2,
  Pin,
  Plus,
  RefreshCw,
  Search,
  Square,
  SquareCheckBig,
  UserPlus2,
  Users,
  X,
} from 'lucide-react';
import type { ThreadFilter, ThreadSort } from '@bfc/shared';
import { Button } from '@/core/components/ui/button';
import { Card, CardContent } from '@/core/components/ui/card';
import { Input } from '@/core/components/ui/input';
import { Avatar } from '@/core/components/Avatar';
import { MemberCard } from '@/tools/circle-dm/components/MemberCard';
import { useAccounts, useEnsureActiveAccount } from '@/tools/circle-dm/hooks/useAccounts';
import { useMembers } from '@/tools/circle-dm/hooks/useMembers';
import { useWsEvent } from '@/core/lib/ws';
import { api } from '@/tools/circle-dm/lib/api';
import { formatRelative } from '@/core/lib/format';
import { cn, foldText, textMatches } from '@/core/lib/utils';
import { useRegisterAssistantContext } from '@/tools/circle-dm/assistant/AssistantContext';
import { useToast } from '@/core/components/ui/toast';
import { setBulkQueue } from '@/tools/circle-dm/lib/bulk-queue';

interface FilterMeta {
  value: ThreadFilter;
  label: string;
  // Short tooltip (HTML title)
  hint: string;
  // Long description shown under the filter bar when this filter is active.
  // Takes settings to interpolate dynamic threshold values.
  describe: (s: { noReplyThresholdDays: number; silenceThresholdDays: number }) => string;
}

const FILTERS: FilterMeta[] = [
  {
    value: 'inbox',
    label: 'Inbox',
    hint: 'Twoja domyślna lista do roboty',
    describe: () =>
      'Aktywne wątki które wymagają Twojej uwagi. Pomija: wątki w Done (archiwum) oraz wątki z przyszłym check-up\'em (parkowane do daty due). Wątki z DUE check-upem wracają tu z badge\'em "check-up due".',
  },
  {
    value: 'unread',
    label: 'Nieodpisane',
    hint: 'Oni wysłali ostatnią — czeka Twoja reakcja',
    describe: () =>
      'Wątki gdzie druga strona wysłała ostatnią wiadomość, a Ty jeszcze nie odpowiedziałeś. Done jest pominięty. Jeśli wątku tu nie ma — albo Ty napisałeś ostatni, albo wątek jest zarchiwizowany.',
  },
  {
    value: 'no_reply',
    label: 'Brak odpowiedzi',
    hint: 'Twoja ostatnia wiadomość bez odzewu',
    describe: (s) =>
      `Wątki gdzie TY wysłałeś ostatnią wiadomość i druga strona nie odpisała od minimum ${s.noReplyThresholdDays} dni. Próg ustawiasz w /admin/settings (no_reply_threshold_days, default 3).`,
  },
  {
    value: 'silent',
    label: 'Cisza',
    hint: 'Wątki które wypadły z radaru',
    describe: (s) =>
      `Wątki gdzie żadna ze stron nie pisała od minimum ${s.silenceThresholdDays} dni. Kandydaci na reaktywację ("ożyw kontakt"). Pomija Done (archiwum) i wątki z zaplanowanym przyszłym check-upem (już masz follow-up w queue). Próg edytowalny: silence_threshold_days.`,
  },
  {
    value: 'flagged',
    label: 'Flaga',
    hint: 'Ważne wątki oznaczone ręcznie',
    describe: () =>
      'Wątki ręcznie oflagowane (button "Flaga" w wątku). Niezależne od statusu — pokażą się tu też wątki Done, jeśli wcześniej je oflagowałeś. Flaga to "trzymaj na oku, ważne", w odróżnieniu od circle\'owego Pin (pinned_at z Circle, ortogonalnie).',
  },
  {
    value: 'checkup',
    label: 'Check-up',
    hint: 'Zaplanowane follow-upy',
    describe: () =>
      'Wątki z zaplanowanym co najmniej jednym pending check-up\'em. Możesz mieć kilka per wątek (sekwencja: +2d, +7d, +14d…). Po wysłaniu wiadomości pending check-up\'y automatycznie się odhaczają. Sort "Najbliższy check-up" porządkuje wg najwcześniejszego due_at.',
  },
  {
    value: 'done',
    label: 'Done',
    hint: 'Archiwum',
    describe: () =>
      'Wątki zarchiwizowane buttonem "Done" w nagłówku. Nie pokazują się w Inbox / Nieodpisane / Brak odpowiedzi / Cisza. Auto-revival: gdy klient prześle nową wiadomość, wątek WRACA do Inbox sam — żeby nie zgubić odzewu.',
  },
];

const SORTS: { value: ThreadSort; label: string }[] = [
  { value: 'recent', label: 'Najnowsze' },
  { value: 'oldest_no_reply', label: 'Najdłużej bez odpowiedzi' },
  { value: 'next_checkup', label: 'Najbliższy check-up' },
];

export function InboxPage() {
  const { data: accounts } = useAccounts();
  const activeId = useEnsureActiveAccount();
  // Domyślnie "Nieodpisane" (nie Inbox): po odpisaniu wątek sam wypada z listy,
  // więc nie trzeba przeskakiwać między zakładkami (uwaga Krystiana).
  const [filter, setFilter] = useState<ThreadFilter>('unread');
  const [sort, setSort] = useState<ThreadSort>('recent');
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [selectedMembers, setSelectedMembers] = useState<Map<number, CommunityMember>>(new Map());
  const [membersOpen, setMembersOpen] = useState(false);
  const [membersSearch, setMembersSearch] = useState('');
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const navigate = useNavigate();

  const threadsQuery = useQuery({
    queryKey: ['threads', activeId, filter, sort],
    queryFn: () =>
      activeId !== null
        ? api.threads.list({ adminAccountId: activeId, filter, sort, limit: 200 })
        : Promise.resolve({ threads: [], count: 0 }),
    enabled: activeId !== null,
  });

  const settingsQuery = useQuery({
    queryKey: ['settings'],
    queryFn: () => api.settings.get(),
    staleTime: 60_000,
  });
  const thresholds = {
    noReplyThresholdDays: settingsQuery.data?.noReplyThresholdDays ?? 3,
    silenceThresholdDays: settingsQuery.data?.silenceThresholdDays ?? 14,
  };
  const activeFilter = FILTERS.find((f) => f.value === filter);

  useWsEvent('threads:updated', (event) => {
    if (event.adminAccountId === activeId) {
      queryClient.invalidateQueries({ queryKey: ['threads', activeId] });
    }
  });

  // Reset selection when filter or account changes (across-context selections
  // would be confusing).
  useEffect(() => {
    setSelected(new Set());
    setSelectedMembers(new Map());
  }, [filter, activeId]);

  const activeAccount = accounts?.find((a) => a.id === activeId) ?? null;
  const allThreads = threadsQuery.data?.threads ?? [];
  const folded = foldText(membersSearch);
  const threads = folded
    ? allThreads.filter(
        (t) =>
          textMatches(t.otherParticipantName, folded) ||
          textMatches(t.chatRoomName, folded) ||
          textMatches(t.otherParticipantEmail, folded) ||
          textMatches(t.lastMessagePreview, folded),
      )
    : allThreads;

  const onSync = async () => {
    if (activeId === null) return;
    try {
      const result = await api.accounts.sync(activeId);
      toast({
        kind: 'success',
        title: 'Zsynchronizowano',
        description: `${result.changedThreadIds.length} wątków odświeżonych`,
      });
      queryClient.invalidateQueries({ queryKey: ['threads', activeId] });
    } catch (err) {
      toast({ kind: 'error', title: 'Błąd sync', description: (err as Error).message });
    }
  };

  const toggleOne = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const selectAll = () => setSelected(new Set(threads.map((t) => t.id)));
  const clearSelection = () => {
    setSelected(new Set());
    setSelectedMembers(new Map());
  };

  const toggleMember = (member: CommunityMember) => {
    setSelectedMembers((prev) => {
      const next = new Map(prev);
      if (next.has(member.id)) next.delete(member.id);
      else next.set(member.id, member);
      return next;
    });
  };

  const totalSelected = selected.size + selectedMembers.size;
  // Once a selection exists, clicking a thread card toggles it instead of
  // opening the chat (Tomasz: "klikam w inne czaty i one się zaznaczają").
  const selectionMode = totalSelected > 0;

  useRegisterAssistantContext(
    useMemo(
      () => ({
        kind: 'inbox' as const,
        adminAccountId: activeId,
        filter,
        sort,
        query: membersSearch,
      }),
      [activeId, filter, sort, membersSearch],
    ),
  );

  const startBulkCompose = () => {
    if (activeId === null || totalSelected === 0) return;
    const selectedThreads = threads.filter((t) => selected.has(t.id));
    setBulkQueue({
      adminAccountId: activeId,
      items: [
        ...selectedThreads.map((t) => ({
          kind: 'thread' as const,
          threadId: t.id,
          name: t.otherParticipantName ?? t.chatRoomName ?? '(bez nazwy)',
          avatarUrl: t.otherParticipantAvatarUrl,
          lastMessagePreview: t.lastMessagePreview,
          lastMessageAt: t.lastMessageAt,
        })),
        ...Array.from(selectedMembers.values()).map((m) => ({
          kind: 'member' as const,
          memberId: m.circleCommunityMemberId,
          name: m.name,
          avatarUrl: m.avatarUrl,
          lastMessagePreview: m.headline,
          lastMessageAt: null,
        })),
      ],
    });
    navigate('/circle-dm/bulk-compose');
  };

  const BULK_LABEL: Record<BulkFolderAction, string> = {
    done: 'Done (archiwum)',
    inbox: 'Inbox',
    flag: 'Flaga',
    unflag: 'bez flagi',
  };

  const bulkActionMutation = useMutation({
    mutationFn: (action: BulkFolderAction) =>
      api.threads.bulkAction(activeId!, Array.from(selected), action),
    onSuccess: (res, action) => {
      queryClient.invalidateQueries({ queryKey: ['threads', activeId] });
      clearSelection();
      toast({
        kind: 'success',
        title: 'Przeniesiono',
        description: `${res.count} ${res.count === 1 ? 'wątek' : 'wątków'} → ${BULK_LABEL[action]}`,
      });
    },
    onError: (err) =>
      toast({ kind: 'error', title: 'Błąd', description: (err as Error).message }),
  });

  const onBulkAction = (action: BulkFolderAction) => {
    if (activeId === null || selected.size === 0) return;
    bulkActionMutation.mutate(action);
  };

  const counts = useMemo(() => threads.length, [threads]);

  if (!accounts) {
    return (
      <div className="flex items-center justify-center py-16 text-foreground/50">
        <Loader2 className="h-5 w-5 animate-spin mr-2" />
        Ładuję…
      </div>
    );
  }

  if (accounts.length === 0) {
    return (
      <div className="max-w-xl mx-auto py-12 text-center animate-fade-in">
        <Card glow>
          <CardContent className="py-10 flex flex-col items-center">
            <UserPlus2 className="h-10 w-10 text-primary mb-4" />
            <h2 className="auth-title text-xl mb-2">Dodaj pierwsze konto</h2>
            <p className="text-foreground/60 mb-6 text-sm max-w-sm">
              Żeby zacząć, podłącz konto admina Circle przy użyciu Headless Auth Tokenu.
            </p>
            <Link to="/circle-dm/accounts">
              <Button variant="default">
                <UserPlus2 className="h-4 w-4" />
                Przejdź do kont
              </Button>
            </Link>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="animate-fade-in pb-24">
      <div className="flex flex-wrap items-end justify-between gap-4 mb-5">
        <div>
          <h1 className="auth-title text-2xl mb-1 flex items-center gap-3">
            <Inbox className="h-6 w-6 text-primary" />
            Inbox
          </h1>
          <p className="text-sm text-foreground/60">
            {activeAccount ? (
              <>
                Konto: <span className="text-foreground">{activeAccount.label}</span>
                {' · '}
                <span className="text-foreground/50">
                  {counts} {counts === 1 ? 'wątek' : counts < 5 ? 'wątki' : 'wątków'}
                </span>
              </>
            ) : (
              'Wybierz konto aktywne w zakładce Konta.'
            )}
          </p>
        </div>
        <div className="flex gap-2 flex-wrap">
          {accounts.length > 1 && <AccountSwitcher activeId={activeId} accounts={accounts} />}
          <Button variant="outline" size="sm" onClick={onSync} disabled={!activeId}>
            <RefreshCw className="h-4 w-4" />
            Synchronizuj
          </Button>
          <Link to="/circle-dm/compose">
            <Button variant="default" size="sm">
              <Plus className="h-4 w-4" />
              Nowa wiadomość
            </Button>
          </Link>
        </div>
      </div>

      <div className="relative mb-3">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-foreground/40" />
        <Input
          placeholder="Szukaj wszędzie: wątki i członkowie (ignoruje wielkość liter i polskie znaki)…"
          value={membersSearch}
          onChange={(e) => setMembersSearch(e.target.value)}
          className="pl-9"
        />
        {membersSearch && (
          <button
            type="button"
            onClick={() => setMembersSearch('')}
            className="absolute right-3 top-1/2 -translate-y-1/2 text-foreground/40 hover:text-foreground"
            title="Wyczyść wyszukiwanie"
          >
            <X className="h-4 w-4" />
          </button>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2 mb-3">
        {FILTERS.map((f) => (
          <button
            key={f.value}
            type="button"
            onClick={() => setFilter(f.value)}
            title={f.hint}
            className={cn(
              'text-sm font-medium px-3.5 py-1.5 rounded-full border transition-colors',
              filter === f.value
                ? 'bg-primary/15 border-primary/40 text-primary'
                : 'border-border text-foreground/70 hover:text-foreground hover:bg-foreground/5',
            )}
          >
            {f.label}
          </button>
        ))}
        <div className="flex-1" />
        <SortDropdown value={sort} onChange={setSort} />
      </div>

      {activeFilter && (
        <div className="flex flex-col gap-2 mb-3 px-3 py-2 rounded-md bg-foreground/5 border border-border text-xs text-foreground/70 leading-relaxed">
          <div className="flex items-start gap-2">
            <Info className="h-3.5 w-3.5 shrink-0 mt-0.5 text-primary/70" />
            <p>
              <span className="font-semibold text-foreground">{activeFilter.label}:</span>{' '}
              {activeFilter.describe(thresholds)}
            </p>
          </div>
          {filter === 'no_reply' && (
            <ThresholdEditor
              field="noReplyThresholdDays"
              currentValue={thresholds.noReplyThresholdDays}
              label="Brak odpowiedzi"
            />
          )}
          {filter === 'silent' && (
            <ThresholdEditor
              field="silenceThresholdDays"
              currentValue={thresholds.silenceThresholdDays}
              label="Cisza"
            />
          )}
        </div>
      )}

      {threads.length > 0 && (
        <div className="flex items-center gap-2 mb-3 text-xs text-foreground/50">
          <button
            type="button"
            className="flex items-center gap-1.5 hover:text-foreground transition-colors"
            onClick={selected.size === threads.length ? clearSelection : selectAll}
          >
            {selected.size === threads.length ? (
              <SquareCheckBig className="h-3.5 w-3.5 text-primary" />
            ) : (
              <Square className="h-3.5 w-3.5" />
            )}
            {selected.size === threads.length
              ? 'Odznacz wszystkie'
              : selected.size > 0
                ? `Zaznacz wszystkie (${threads.length})`
                : `Zaznacz wszystkie z filtra "${FILTERS.find((f) => f.value === filter)?.label}"`}
          </button>
          {selected.size > 0 && (
            <>
              <span>·</span>
              <span className="text-primary font-medium">{selected.size} zaznaczonych</span>
            </>
          )}
        </div>
      )}

      {threadsQuery.isLoading && (
        <div className="flex items-center justify-center py-16 text-foreground/50">
          <Loader2 className="h-5 w-5 animate-spin mr-2" />
          Ładuję wątki…
        </div>
      )}

      {threadsQuery.data && threads.length === 0 && (
        <Card>
          <CardContent className="py-10 text-center text-foreground/60">
            {membersSearch
              ? `Żaden wątek nie pasuje do "${membersSearch}". Sprawdź listę członków niżej.`
              : 'Brak wątków w tej kategorii.'}
          </CardContent>
        </Card>
      )}

      <div className="flex flex-col gap-2">
        {threads.map((t, idx) => {
          const waitingForReply = t.lastMessageSenderIsMe && t.status !== 'done';
          const isSelected = selected.has(t.id);
          const checkupDueAt = t.nextCheckupDueAt ? new Date(t.nextCheckupDueAt) : null;
          const checkupDue = checkupDueAt !== null && checkupDueAt.getTime() <= Date.now();
          return (
            <div
              key={t.id}
              className="block animate-fade-in relative group"
              style={{ animationDelay: `${Math.min(idx, 20) * 20}ms` }}
            >
              <Card
                className={cn(
                  'hover:bg-card-hover transition-colors',
                  isSelected && 'border-primary/40 bg-primary/5',
                )}
              >
                <CardContent className="flex items-center gap-3 p-3 sm:p-4">
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      toggleOne(t.id);
                    }}
                    className={cn(
                      'shrink-0 h-5 w-5 rounded border-2 grid place-items-center transition-colors',
                      isSelected
                        ? 'border-primary bg-primary/20'
                        : 'border-border hover:border-primary/60',
                    )}
                    title={isSelected ? 'Odznacz' : 'Zaznacz'}
                  >
                    {isSelected && <CheckCheck className="h-3 w-3 text-primary" />}
                  </button>

                  <CardClickable
                    selectionMode={selectionMode}
                    threadId={t.id}
                    onSelect={() => toggleOne(t.id)}
                  >
                    <Avatar
                      name={t.otherParticipantName ?? t.chatRoomName}
                      url={t.otherParticipantAvatarUrl}
                      size="md"
                    />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 mb-0.5">
                        <span className="font-semibold truncate text-foreground">
                          {t.otherParticipantName ?? t.chatRoomName ?? '(bez nazwy)'}
                        </span>
                        {t.chatRoomKind === 'group_chat' && (
                          <span className="badge-info text-[10px] uppercase px-1.5 py-0.5 rounded">
                            group
                          </span>
                        )}
                        {t.pinnedAt && <Pin className="h-3 w-3 text-primary" />}
                        {t.isFlagged && (
                          <span title="Oflagowane" className="inline-flex">
                            <Flag className="h-3 w-3 text-warning" />
                          </span>
                        )}
                        {t.status === 'done' && (
                          <span
                            className="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded bg-foreground/10 text-foreground/50"
                            title="Wątek zarchiwizowany"
                          >
                            done
                          </span>
                        )}
                        {checkupDue && (
                          <span
                            className="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded bg-primary/20 text-primary flex items-center gap-1"
                            title={`Check-up DUE${t.nextCheckupNote ? `: ${t.nextCheckupNote}` : ''}`}
                          >
                            <Clock3 className="h-3 w-3" />
                            check-up due
                          </span>
                        )}
                        {waitingForReply && (
                          <span
                            className="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded bg-warning/15 text-warning flex items-center gap-1"
                            title="Wysłaliśmy ostatnią wiadomość — druga strona jeszcze nie odpowiedziała"
                          >
                            <Clock3 className="h-3 w-3" />
                            brak odpowiedzi
                          </span>
                        )}
                        {t.unreadMessagesCount > 0 && (
                          <span className="ml-auto badge-brand text-[11px] font-bold px-2 py-0.5 rounded-full">
                            {t.unreadMessagesCount}
                          </span>
                        )}
                        <span
                          className={cn(
                            'text-xs shrink-0',
                            t.unreadMessagesCount > 0 ? 'text-foreground' : 'text-foreground/40',
                            !(t.unreadMessagesCount > 0) && 'ml-auto',
                          )}
                        >
                          {formatRelative(t.lastMessageAt)}
                        </span>
                      </div>
                      <p
                        className={cn(
                          'text-sm truncate',
                          t.lastMessageSenderIsMe ? 'text-foreground/45' : 'text-foreground/75',
                        )}
                      >
                        {t.lastMessageSenderIsMe && (
                          <span className="text-foreground/40 mr-1">Ty:</span>
                        )}
                        {t.lastMessagePreview ?? '—'}
                      </p>
                    </div>
                  </CardClickable>
                </CardContent>
              </Card>
            </div>
          );
        })}
      </div>

      <MembersSection
        adminAccountId={activeId}
        open={membersOpen}
        setOpen={setMembersOpen}
        search={membersSearch}
        selectedMembers={selectedMembers}
        selectionMode={selectionMode}
        onToggleMember={toggleMember}
      />

      {totalSelected > 0 && (
        <BulkActionBar
          count={totalSelected}
          threadCount={selected.size}
          busy={bulkActionMutation.isPending}
          onClear={clearSelection}
          onCompose={startBulkCompose}
          onBulkAction={onBulkAction}
        />
      )}
    </div>
  );
}

type BulkFolderAction = 'done' | 'inbox' | 'flag' | 'unflag';

const FOLDER_ACTIONS: {
  action: BulkFolderAction;
  label: string;
  icon: typeof Archive;
}[] = [
  { action: 'done', label: 'Do Done (archiwum)', icon: Archive },
  { action: 'inbox', label: 'Przywróć do Inboxa', icon: Inbox },
  { action: 'flag', label: 'Oznacz flagą', icon: Flag },
  { action: 'unflag', label: 'Zdejmij flagę', icon: FlagOff },
];

function BulkActionBar({
  count,
  threadCount,
  busy,
  onClear,
  onCompose,
  onBulkAction,
}: {
  count: number;
  threadCount: number;
  busy: boolean;
  onClear: () => void;
  onCompose: () => void;
  onBulkAction: (action: BulkFolderAction) => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const memberOnly = threadCount === 0;

  return (
    <div className="fixed bottom-4 inset-x-0 z-30 flex justify-center pointer-events-none">
      <div className="pointer-events-auto animate-fade-in-up flex items-center gap-3 bg-card border border-primary/40 shadow-glow-card rounded-full px-4 py-2">
        <span className="text-sm font-semibold text-primary px-2">
          Zaznaczono {count} {count === 1 ? 'osobę' : 'osób'}
        </span>
        <Button variant="ghost" size="sm" onClick={onClear}>
          <X className="h-4 w-4" />
          Wyczyść
        </Button>

        <div className="relative">
          <Button
            variant="ghost"
            size="sm"
            disabled={memberOnly || busy}
            title={
              memberOnly
                ? 'Foldery działają tylko na istniejących wątkach (nie na nowych członkach)'
                : 'Przenieś zaznaczone wątki do folderu/tagu'
            }
            onClick={() => setMenuOpen((v) => !v)}
          >
            {busy ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <FolderInput className="h-4 w-4" />
            )}
            Przenieś{threadCount > 0 ? ` (${threadCount})` : ''}
            <ChevronDown className="h-4 w-4" />
          </Button>
          {menuOpen && !memberOnly && (
            <>
              <div className="fixed inset-0 z-10" onClick={() => setMenuOpen(false)} />
              <div className="absolute bottom-full left-0 mb-2 z-20 w-56 rounded-lg border border-border bg-card p-1 shadow-lg">
                {FOLDER_ACTIONS.map(({ action, label, icon: Icon }) => (
                  <button
                    key={action}
                    type="button"
                    className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm text-foreground/80 hover:bg-foreground/10"
                    onClick={() => {
                      setMenuOpen(false);
                      onBulkAction(action);
                    }}
                  >
                    <Icon className="h-4 w-4 text-foreground/50" />
                    {label}
                  </button>
                ))}
              </div>
            </>
          )}
        </div>

        <Button variant="default" size="sm" onClick={onCompose}>
          Napisz do zaznaczonych
          <ArrowRight className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}

function CardClickable({
  selectionMode,
  threadId,
  onSelect,
  children,
}: {
  selectionMode: boolean;
  threadId: number;
  onSelect: () => void;
  children: ReactNode;
}) {
  const cls = 'flex items-center gap-3 flex-1 min-w-0 text-left';
  if (selectionMode) {
    return (
      <button
        type="button"
        onClick={onSelect}
        className={cls}
        title="Tryb zaznaczania — klik zaznacza/odznacza wątek"
      >
        {children}
      </button>
    );
  }
  return (
    <Link to={`/circle-dm/thread/${threadId}`} className={cls}>
      {children}
    </Link>
  );
}

function SortDropdown({
  value,
  onChange,
}: {
  value: ThreadSort;
  onChange: (v: ThreadSort) => void;
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value as ThreadSort)}
      className="h-9 px-3 rounded-md border border-input bg-background/40 text-sm focus-visible:ring-2 focus-visible:ring-ring outline-none"
      title="Sortowanie"
    >
      {SORTS.map((s) => (
        <option key={s.value} value={s.value}>
          {s.label}
        </option>
      ))}
    </select>
  );
}

function MembersSection({
  adminAccountId,
  open,
  setOpen,
  search,
  selectedMembers,
  selectionMode,
  onToggleMember,
}: {
  adminAccountId: number | null;
  open: boolean;
  setOpen: (v: boolean | ((prev: boolean) => boolean)) => void;
  search: string;
  selectedMembers: Map<number, CommunityMember>;
  selectionMode: boolean;
  onToggleMember: (member: CommunityMember) => void;
}) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const folded = foldText(search);
  // Expand automatically while searching so one search covers both groups.
  const expanded = open || folded.length > 0;
  // Fetch the full cached list (no server q) and filter client-side so the
  // match is diacritic- and case-insensitive (server ilike isn't).
  // Exclude members who already have a thread — they show up top, no point
  // duplicating them in "Pozostali" (= ludzie z którymi jeszcze nie pisałeś).
  const membersQuery = useMembers(expanded ? adminAccountId : null, '', {
    excludeWithThread: true,
  });
  const members = (membersQuery.data ?? []).filter(
    (m) =>
      textMatches(m.name, folded) ||
      textMatches(m.email, folded) ||
      textMatches(m.headline, folded),
  );

  const handleRefresh = async () => {
    if (adminAccountId === null) return;
    try {
      const result = await api.members.sync(adminAccountId);
      queryClient.invalidateQueries({ queryKey: ['members', adminAccountId] });
      toast({ kind: 'success', title: `Odświeżono`, description: `${result.syncedCount} osób` });
    } catch (err) {
      toast({ kind: 'error', title: 'Sync error', description: (err as Error).message });
    }
  };

  return (
    <section className="mt-10 pt-8 border-t border-border">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-3 w-full text-left mb-1 group"
      >
        <Users className="h-5 w-5 text-foreground/60 group-hover:text-foreground transition-colors" />
        <h2 className="auth-title text-xl">Pozostali członkowie</h2>
        <span className="text-xs text-foreground/40 ml-2">
          {expanded ? 'Schowaj' : 'Pokaż listę — napisz do kogoś z kim jeszcze nie pisałeś'}
        </span>
        <ChevronDown
          className={cn(
            'h-5 w-5 text-foreground/50 ml-auto transition-transform',
            expanded && 'rotate-180',
          )}
        />
      </button>

      {expanded && (
        <div className="mt-4 animate-fade-in">
          <div className="flex justify-end mb-4">
            <Button variant="outline" size="sm" onClick={handleRefresh}>
              <RefreshCw className="h-4 w-4" />
              Odśwież listę
            </Button>
          </div>

          {membersQuery.isLoading && (
            <div className="flex items-center justify-center py-12 text-foreground/50">
              <Loader2 className="h-5 w-5 animate-spin mr-2" />
              Ładuję członków…
            </div>
          )}

          {membersQuery.data && members.length === 0 && (
            <Card>
              <CardContent className="py-8 text-center text-foreground/60">
                {search
                  ? `Nic nie pasuje do "${search}"`
                  : 'Brak członków w cache. Kliknij "Odśwież listę".'}
              </CardContent>
            </Card>
          )}

          {members.length > 0 && (
            <>
              <div className="text-xs text-foreground/40 mb-3">
                {members.length} {members.length === 1 ? 'osoba' : 'osób'}
                {' · klik aby napisać nową wiadomość'}
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
                {members.map((m, idx) => (
                  <div
                    key={m.id}
                    className="animate-fade-in"
                    style={{ animationDelay: `${Math.min(idx, 30) * 15}ms` }}
                  >
                    <MemberCard
                      member={m}
                      selectionMode={selectionMode}
                      selectable={{
                        isSelected: selectedMembers.has(m.id),
                        onToggle: () => onToggleMember(m),
                      }}
                    />
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      )}
    </section>
  );
}

function AccountSwitcher({
  activeId,
  accounts,
}: {
  activeId: number | null;
  accounts: Array<{ id: number; label: string; isActive: boolean }>;
}) {
  return (
    <select
      value={activeId ?? ''}
      onChange={(e) => {
        const id = Number.parseInt(e.target.value, 10);
        if (Number.isInteger(id)) {
          import('@/tools/circle-dm/lib/account-context').then((m) => m.setActiveAccountId(id));
        }
      }}
      className="h-9 px-3 rounded-md border border-input bg-background/40 text-sm focus-visible:ring-2 focus-visible:ring-ring outline-none"
    >
      {accounts.map((a) => (
        <option key={a.id} value={a.id}>
          {a.label}
        </option>
      ))}
    </select>
  );
}

const PRESETS: Record<'noReplyThresholdDays' | 'silenceThresholdDays', number[]> = {
  noReplyThresholdDays: [1, 2, 3, 5, 7],
  silenceThresholdDays: [7, 14, 21, 30, 60, 90],
};

function ThresholdEditor({
  field,
  currentValue,
  label,
}: {
  field: 'noReplyThresholdDays' | 'silenceThresholdDays';
  currentValue: number;
  label: string;
}) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [customValue, setCustomValue] = useState('');

  const saveMutation = useMutation({
    mutationFn: (value: number) => api.settings.update({ [field]: value }),
    onSuccess: (_, value) => {
      queryClient.invalidateQueries({ queryKey: ['settings'] });
      queryClient.invalidateQueries({ queryKey: ['threads'] });
      toast({ kind: 'success', title: 'Zapisano', description: `${label}" = ${value} dni` });
      setCustomValue('');
    },
    onError: (e) =>
      toast({ kind: 'error', title: 'Błąd zapisu', description: (e as Error).message }),
  });

  const presets = PRESETS[field];
  const isCustom = !presets.includes(currentValue);

  const saveCustom = () => {
    const parsed = Number.parseInt(customValue, 10);
    if (!Number.isInteger(parsed) || parsed < 1 || parsed > 365) {
      toast({ kind: 'error', title: 'Nieprawidłowa wartość', description: '1–365 dni' });
      return;
    }
    saveMutation.mutate(parsed);
  };

  return (
    <div className="flex flex-wrap items-center gap-1.5 pl-5">
      <span className="text-foreground/50 text-[11px] mr-1">Próg (dni):</span>
      {presets.map((d) => {
        const active = d === currentValue;
        return (
          <button
            key={d}
            type="button"
            disabled={saveMutation.isPending}
            onClick={() => {
              if (!active) saveMutation.mutate(d);
            }}
            className={cn(
              'px-2 py-0.5 rounded-full border text-[11px] font-medium transition-colors',
              active
                ? 'bg-primary/20 border-primary/40 text-primary'
                : 'border-border text-foreground/60 hover:text-foreground hover:bg-foreground/10',
            )}
          >
            {d}
          </button>
        );
      })}
      <span className="text-foreground/30 mx-1">·</span>
      <input
        type="number"
        min={1}
        max={365}
        placeholder={isCustom ? String(currentValue) : 'własne'}
        value={customValue}
        onChange={(e) => setCustomValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            saveCustom();
          }
        }}
        className="w-16 h-6 px-2 rounded border border-border bg-background/40 text-[11px] focus-visible:ring-1 focus-visible:ring-ring outline-none"
      />
      <button
        type="button"
        disabled={!customValue || saveMutation.isPending}
        onClick={saveCustom}
        className="px-2 py-0.5 rounded border border-primary/40 bg-primary/10 text-primary text-[11px] font-semibold hover:bg-primary/20 disabled:opacity-40 disabled:cursor-not-allowed"
      >
        Zapisz
      </button>
      {isCustom && (
        <span className="text-[10px] text-foreground/40 ml-1">
          aktualnie: <span className="text-foreground">{currentValue}</span>
        </span>
      )}
    </div>
  );
}
