import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertTriangle,
  Archive,
  ArchiveRestore,
  ArrowLeft,
  Check,
  Clock3,
  Flag,
  Loader2,
  RefreshCw,
  RotateCcw,
  Send,
  Sparkles,
  Trash2,
  Wand2,
} from 'lucide-react';
// (kept Wand2/Sparkles imports for icon usage)
import {
  type DmMessage,
  type DmThread,
  type DraftSession,
  formatImageForAi,
  formatVoiceForAi,
} from '@bfc/shared';
import { Button } from '@/core/components/ui/button';
import { Card, CardContent } from '@/core/components/ui/card';
import { useAccounts, useActiveAccountId } from '@/tools/circle-dm/hooks/useAccounts';
import { useRegisterAssistantContext } from '@/tools/circle-dm/assistant/AssistantContext';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/core/components/ui/dialog';
import { Textarea } from '@/core/components/ui/textarea';
import { Avatar } from '@/core/components/Avatar';
import { useToast } from '@/core/components/ui/toast';
import { api } from '@/tools/circle-dm/lib/api';
import { MessageAttachments } from '@/tools/circle-dm/components/MessageAttachments';
import { useWsEvent, useWsEvents } from '@/core/lib/ws';
import { formatRelative, formatDateTime } from '@/core/lib/format';
import { cn } from '@/core/lib/utils';

export function ThreadPage() {
  const { id } = useParams<{ id: string }>();
  const threadId = Number.parseInt(id ?? '', 10);

  if (!Number.isInteger(threadId)) {
    return <ThreadError message="Nieprawidłowy id wątku" />;
  }

  return <ThreadView threadId={threadId} />;
}

function ThreadError({ message }: { message: string }) {
  return (
    <div className="max-w-md mx-auto py-12">
      <Card>
        <CardContent className="py-8 text-center">
          <AlertTriangle className="h-8 w-8 text-destructive mx-auto mb-3" />
          <p className="text-foreground/80 mb-4">{message}</p>
          <Link to="/circle-dm">
            <Button variant="outline" size="sm">
              <ArrowLeft className="h-4 w-4" />
              Inbox
            </Button>
          </Link>
        </CardContent>
      </Card>
    </div>
  );
}

function ThreadView({ threadId }: { threadId: number }) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const activeAccountId = useActiveAccountId();
  // null = dialog closed; string = dialog open with that body (frozen at click).
  const [pendingSendBody, setPendingSendBody] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);

  const threadQuery = useQuery({
    queryKey: ['thread', threadId],
    queryFn: () => api.threads.get(threadId),
  });

  const messagesQuery = useQuery({
    queryKey: ['thread', threadId, 'messages'],
    queryFn: () => api.threads.messages(threadId).then((r) => r.messages),
  });

  const draftQuery = useQuery({
    queryKey: ['draft', threadId],
    queryFn: () => api.drafts.get(threadId),
  });

  const { data: accounts } = useAccounts();
  // DraftPanel owns the textarea state; it pushes here so the assistant
  // context reflects what the user actually sees (gotcha #14), not the
  // debounced DB copy which lags.
  const [liveDraftText, setLiveDraftText] = useState('');
  useRegisterAssistantContext(
    useMemo(() => {
      const thread = threadQuery.data;
      if (!thread) return { kind: 'none' as const };
      const acc = accounts?.find((a) => a.id === thread.adminAccountId);
      const msgs = messagesQuery.data ?? [];
      const tail = msgs.slice(-20);
      const historyExcerpt = tail
        .map((m) => {
          const who = m.senderIsMe ? 'ja' : m.senderName ?? 'on/ona';
          const parts: string[] = [];
          const body = (m.body ?? '').slice(0, 600);
          if (body) parts.push(body);
          if (m.voiceTranscriptStatus !== null) {
            parts.push(
              formatVoiceForAi(
                m.voiceDurationSec,
                m.voiceTranscriptStatus,
                m.voiceTranscript,
              ),
            );
          }
          for (const d of m.imageDescriptions ?? []) {
            parts.push(formatImageForAi(d.status, d.description));
          }
          return `${who}: ${parts.join(' ')}`;
        })
        .join('\n');
      return {
        kind: 'thread' as const,
        adminAccountId: thread.adminAccountId,
        threadId: thread.id,
        recipientName: thread.otherParticipantName ?? thread.chatRoomName ?? null,
        persona: acc?.systemPrompt ?? '',
        accountLabel: acc?.label ?? '',
        draftText: liveDraftText,
        historyExcerpt,
      };
    }, [threadQuery.data, messagesQuery.data, liveDraftText, accounts]),
  );

  useWsEvent('messages:loaded', (event) => {
    if (event.threadId === threadId) {
      queryClient.invalidateQueries({ queryKey: ['thread', threadId, 'messages'] });
    }
  });

  useWsEvent('message:transcript_ready', (event) => {
    if (event.threadId === threadId) {
      queryClient.invalidateQueries({ queryKey: ['thread', threadId, 'messages'] });
    }
  });

  useWsEvent('message:image_description_ready', (event) => {
    if (event.threadId === threadId) {
      queryClient.invalidateQueries({ queryKey: ['thread', threadId, 'messages'] });
    }
  });

  useWsEvent('send:result', (event) => {
    if (event.threadId !== threadId) return;
    if (event.ok) {
      toast({
        kind: 'success',
        title: 'Wysłano',
        description: `Circle message_id: ${event.circleMessageId}`,
      });
    } else {
      toast({ kind: 'error', title: 'Wysyłka nieudana', description: event.error });
    }
    queryClient.invalidateQueries({ queryKey: ['thread', threadId, 'messages'] });
    queryClient.invalidateQueries({ queryKey: ['draft', threadId] });
    queryClient.invalidateQueries({ queryKey: ['threads'] });
  });

  const onSync = async () => {
    const accountId = activeAccountId ?? threadQuery.data?.adminAccountId ?? null;
    if (accountId === null) return;
    setSyncing(true);
    try {
      const result = await api.accounts.sync(accountId);
      toast({
        kind: 'success',
        title: 'Zsynchronizowano',
        description: `${result.changedThreadIds.length} wątków odświeżonych`,
      });
      queryClient.invalidateQueries({ queryKey: ['thread', threadId, 'messages'] });
      queryClient.invalidateQueries({ queryKey: ['threads', accountId] });
    } catch (err) {
      toast({ kind: 'error', title: 'Błąd sync', description: (err as Error).message });
    } finally {
      setSyncing(false);
    }
  };

  const sendMutation = useMutation({
    mutationFn: (body: string) => api.drafts.send(threadId, body),
    onSuccess: (data) => {
      if (!data.ok) {
        toast({ kind: 'error', title: 'Wysyłka odrzucona przez Circle', description: data.error });
      }
      setPendingSendBody(null);
    },
    onError: (err) => {
      toast({ kind: 'error', title: 'Send error', description: (err as Error).message });
      setPendingSendBody(null);
    },
  });

  const thread = threadQuery.data;

  if (threadQuery.isLoading) {
    return (
      <div className="flex items-center justify-center py-16 text-foreground/50">
        <Loader2 className="h-5 w-5 animate-spin mr-2" /> Ładuję wątek…
      </div>
    );
  }

  if (!thread) return <ThreadError message="Wątek nie znaleziony" />;

  return (
    <div className="animate-fade-in">
      <div className="flex items-center justify-between gap-3 mb-4 flex-wrap">
        <div className="flex items-center gap-3 min-w-0">
          <Link to="/circle-dm">
            <Button variant="ghost" size="sm">
              <ArrowLeft className="h-4 w-4" />
              Inbox
            </Button>
          </Link>
          <Avatar
            name={thread.otherParticipantName ?? thread.chatRoomName}
            url={thread.otherParticipantAvatarUrl}
            size="md"
          />
          <div className="min-w-0">
            <h1 className="font-bold text-lg truncate">
              {thread.otherParticipantName ?? thread.chatRoomName ?? '(bez nazwy)'}
            </h1>
            <p className="text-xs text-foreground/50 truncate">
              {thread.otherParticipantEmail ?? thread.circleChatRoomUuid}
            </p>
          </div>
        </div>
        <div className="flex gap-2 flex-wrap">
          <ThreadToolbar thread={thread} />
          <Button variant="outline" size="sm" onClick={onSync} disabled={syncing}>
            <RefreshCw className={cn('h-4 w-4', syncing && 'animate-spin')} />
            Synchronizuj
          </Button>
        </div>
      </div>

      <div className="grid lg:grid-cols-[1fr_minmax(360px,460px)] gap-4">
        <MessageHistory
          threadId={threadId}
          messages={messagesQuery.data ?? []}
          loading={messagesQuery.isLoading}
        />
        <DraftPanel
          threadId={threadId}
          initialSession={draftQuery.data?.session ?? null}
          onRequestSend={(body) => setPendingSendBody(body)}
          onTextChange={setLiveDraftText}
        />
      </div>

      <Dialog
        open={pendingSendBody !== null}
        onOpenChange={(open) => {
          if (!open && !sendMutation.isPending) setPendingSendBody(null);
        }}
      >
        <SendConfirmDialog
          body={pendingSendBody ?? ''}
          onCancel={() => setPendingSendBody(null)}
          onConfirm={() => {
            if (pendingSendBody) sendMutation.mutate(pendingSendBody);
          }}
          pending={sendMutation.isPending}
        />
      </Dialog>
    </div>
  );
}

function MessageHistory({
  threadId,
  messages,
  loading,
}: {
  threadId: number;
  messages: DmMessage[];
  loading: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [messages.length]);

  if (loading && messages.length === 0) {
    return (
      <Card className="min-h-[50vh] grid place-items-center">
        <Loader2 className="h-5 w-5 animate-spin text-foreground/40" />
      </Card>
    );
  }

  if (messages.length === 0) {
    return (
      <Card>
        <CardContent className="py-12 text-center text-foreground/60">
          Brak wiadomości w tym wątku.
        </CardContent>
      </Card>
    );
  }

  // Read-state proxy: if the most recent message is ours, we're waiting for a reply.
  const lastIdx = messages.length - 1;
  const waitingForReply = lastIdx >= 0 && messages[lastIdx]!.senderIsMe;

  return (
    <Card>
      <CardContent
        ref={ref}
        className="flex flex-col gap-3 p-4 max-h-[calc(100vh-200px)] overflow-y-auto"
      >
        {messages.map((m, i) => (
          <div
            key={m.id}
            className={cn(
              'flex flex-col max-w-[85%]',
              m.senderIsMe ? 'items-end self-end' : 'items-start',
            )}
          >
            <div className="flex items-center gap-2 text-[11px] text-foreground/40 mb-1">
              <span>{m.senderIsMe ? 'Ty' : m.senderName ?? '—'}</span>
              <span>·</span>
              <span title={formatDateTime(m.createdAt)}>{formatRelative(m.createdAt)}</span>
            </div>
            {m.body && (
              <div
                className={cn(
                  'rounded-lg px-3 py-2 text-sm whitespace-pre-wrap break-words',
                  m.senderIsMe
                    ? 'bg-primary/15 text-foreground border border-primary/20'
                    : 'bg-card-hover text-foreground border border-border',
                )}
              >
                {m.body}
              </div>
            )}
            <MessageAttachments
              attachments={m.attachments ?? []}
              isMine={m.senderIsMe}
              messageId={m.id}
              threadId={threadId}
              voice={
                m.voiceTranscriptStatus
                  ? {
                      messageId: m.id,
                      threadId,
                      transcript: m.voiceTranscript,
                      status: m.voiceTranscriptStatus,
                      error: m.voiceTranscriptError,
                      durationSec: m.voiceDurationSec,
                    }
                  : undefined
              }
              imageDescriptions={m.imageDescriptions}
            />
            {i === lastIdx && waitingForReply && (
              <div className="text-[10px] text-foreground/35 mt-1 italic" title="Brak odpowiedzi — uznajemy za nieprzeczytane">
                ⏳ Czeka na odpowiedź
              </div>
            )}
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

function DraftPanel({
  threadId,
  initialSession,
  onRequestSend,
  onTextChange,
}: {
  threadId: number;
  initialSession: DraftSession | null;
  onRequestSend: (body: string) => void;
  /** Live mirror of the textarea up to the parent (for assistant context). */
  onTextChange?: (text: string) => void;
}) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [text, setText] = useState(initialSession?.currentDraft ?? '');

  // Push every change to the parent. Cheap, runs in effect to avoid setState-
  // during-render and to ignore intermediate stream tokens before debounced
  // save (the parent observes the same value either way).
  useEffect(() => {
    onTextChange?.(text);
  }, [text, onTextChange]);
  const [status, setStatus] = useState(initialSession?.status ?? 'idle');
  const [streamingKind, setStreamingKind] = useState<'initial' | 'format' | null>(null);
  const [lastResultKind, setLastResultKind] = useState<'draft' | 'format' | null>(null);

  useEffect(() => {
    if (initialSession) {
      setText(initialSession.currentDraft ?? '');
      setStatus(initialSession.status);
    }
  }, [initialSession]);

  // Buffer streamed tokens locally for the auto-generate flow
  const streamBufferRef = useRef('');

  useWsEvents((event) => {
    if (!('threadId' in event)) return;
    if (event.type !== 'send:result' && event.threadId !== threadId) return;

    switch (event.type) {
      case 'draft:status':
        setStatus(event.status);
        if (event.status === 'generating') {
          streamBufferRef.current = '';
          setStreamingKind('initial');
          setLastResultKind(null);
        }
        if (event.status === 'sent') {
          setText('');
          streamBufferRef.current = '';
          setStreamingKind(null);
          setLastResultKind(null);
        }
        break;
      case 'draft:token':
        streamBufferRef.current += event.chunk;
        setText(streamBufferRef.current);
        break;
      case 'draft:complete':
        setText(event.draft);
        setStatus('has_draft');
        setStreamingKind(null);
        setLastResultKind('draft');
        queryClient.invalidateQueries({ queryKey: ['draft', threadId] });
        break;
      case 'draft:tool_use':
        break;
    }
  });

  const generateMutation = useMutation({
    mutationFn: () => {
      setLastResultKind(null);
      return api.drafts.generate(threadId);
    },
    onError: (err) =>
      toast({ kind: 'error', title: 'Błąd generowania', description: (err as Error).message }),
  });

  const formatMutation = useMutation({
    mutationFn: () => {
      setStreamingKind('format');
      setLastResultKind(null);
      return api.format.thread(threadId, text);
    },
    onSuccess: (r) => {
      setText(r.text);
      setStreamingKind(null);
      setLastResultKind('format');
    },
    onError: (err) => {
      setStreamingKind(null);
      toast({ kind: 'error', title: 'Błąd formatowania', description: (err as Error).message });
    },
  });

  const resetMutation = useMutation({
    mutationFn: () => api.drafts.reset(threadId),
    onSuccess: () => {
      setText('');
      setStatus('idle');
      queryClient.invalidateQueries({ queryKey: ['draft', threadId] });
    },
  });

  // Debounce manual edits → PATCH /drafts/:id (so refresh doesn't lose work)
  useEffect(() => {
    if (streamingKind) return;
    if (text === (initialSession?.currentDraft ?? '')) return;
    const handle = setTimeout(() => {
      void api.drafts.update(threadId, text).catch(() => {});
    }, 800);
    return () => clearTimeout(handle);
  }, [text, streamingKind, threadId, initialSession]);

  const busyGenerating = status === 'generating' || streamingKind === 'initial';
  const busyFormatting = streamingKind === 'format' || formatMutation.isPending;
  const isBusy = busyGenerating || busyFormatting;
  const canSend = text.trim().length > 0 && !isBusy;

  return (
    <Card glow className="h-fit lg:sticky lg:top-20 animate-scale-in">
      <CardContent className="p-4 flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <h2 className="auth-title text-base flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-primary" />
            Wiadomość
          </h2>
          <StatusPill status={status} streamingKind={streamingKind} lastResultKind={lastResultKind} />
        </div>

        <Textarea
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            setLastResultKind(null);
          }}
          onKeyDown={(e) => {
            // Enter = wyślij (otwórz confirm). Shift+Enter = nowa linia.
            if (e.key === 'Enter' && !e.shiftKey && !e.metaKey && !e.ctrlKey) {
              if (text.trim() && !isBusy) {
                e.preventDefault();
                onRequestSend(text);
              }
            }
          }}
          placeholder={
            busyGenerating
              ? 'Generuję draft…'
              : 'Wpisz treść, brain dump z dyktowania, lub edytuj auto-draft.\n\nEnter = wyślij. Shift+Enter = nowa linia. „Formatuj z AI" przerobi tekst zgodnie z personą.'
          }
          rows={12}
          disabled={isBusy}
          className="font-sans text-sm leading-relaxed"
        />

        <div className="grid grid-cols-2 gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => formatMutation.mutate()}
            disabled={isBusy || !text.trim()}
          >
            {busyFormatting ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Wand2 className="h-4 w-4" />
            )}
            Formatuj z AI
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => generateMutation.mutate()}
            disabled={isBusy}
            title="Wygeneruj draft od zera na podstawie historii i persony"
          >
            {generateMutation.isPending ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <RotateCcw className="h-4 w-4" />
            )}
            Wygeneruj draft
          </Button>
        </div>

        <div className="pt-2 border-t border-border">
          <Button
            variant="default"
            className="w-full"
            disabled={!canSend}
            onClick={() => onRequestSend(text)}
          >
            <Send className="h-4 w-4" />
            Wyślij wiadomość
          </Button>
        </div>

        {initialSession && (
          <button
            type="button"
            className="text-xs text-foreground/40 hover:text-foreground/70 transition-colors mt-1 self-start"
            onClick={() => resetMutation.mutate()}
            disabled={isBusy}
          >
            Wyczyść i zacznij od nowa
          </button>
        )}
      </CardContent>
    </Card>
  );
}

function StatusPill({
  status,
  streamingKind,
  lastResultKind,
}: {
  status: string;
  streamingKind: 'initial' | 'format' | null;
  lastResultKind: 'draft' | 'format' | null;
}) {
  if (streamingKind === 'format') {
    return (
      <span className="badge-brand text-[10px] uppercase tracking-wider px-2 py-0.5 rounded-full flex items-center gap-1">
        <Loader2 className="h-3 w-3 animate-spin" /> formatuję
      </span>
    );
  }
  if (status === 'generating' || streamingKind === 'initial') {
    return (
      <span className="badge-brand text-[10px] uppercase tracking-wider px-2 py-0.5 rounded-full flex items-center gap-1">
        <Loader2 className="h-3 w-3 animate-spin" /> generuję
      </span>
    );
  }
  if (status === 'sent') {
    return (
      <span className="badge-success text-[10px] uppercase tracking-wider px-2 py-0.5 rounded-full">
        wysłano
      </span>
    );
  }
  if (status === 'error') {
    return (
      <span className="badge-error text-[10px] uppercase tracking-wider px-2 py-0.5 rounded-full">
        błąd
      </span>
    );
  }
  if (lastResultKind === 'format') {
    return (
      <span className="badge-info text-[10px] uppercase tracking-wider px-2 py-0.5 rounded-full">
        sformatowano
      </span>
    );
  }
  if (status === 'has_draft' || lastResultKind === 'draft') {
    return (
      <span className="badge-info text-[10px] uppercase tracking-wider px-2 py-0.5 rounded-full">
        draft
      </span>
    );
  }
  return null;
}

function SendConfirmDialog({
  body,
  onCancel,
  onConfirm,
  pending,
}: {
  body: string;
  onCancel: () => void;
  onConfirm: () => void;
  pending: boolean;
}) {
  const canSend = !pending && body.trim().length > 0;
  return (
    <DialogContent
      onKeyDown={(e) => {
        // Enter on the dialog confirms (drugi raz po Enter w drafcie = wysłane).
        if (e.key === 'Enter' && !e.shiftKey && canSend) {
          e.preventDefault();
          onConfirm();
        }
      }}
    >
      <DialogHeader>
        <DialogTitle>Wysłać wiadomość?</DialogTitle>
        <DialogDescription>
          Pójdzie do Circle pod kontem admina powiązanym z tym wątkiem. Nie da się jej odwołać.
        </DialogDescription>
      </DialogHeader>
      <div className="rounded-md border border-primary/30 bg-card-hover/50 p-3 max-h-72 overflow-y-auto whitespace-pre-wrap text-sm">
        {body || <span className="text-foreground/50">(pusto)</span>}
      </div>
      <DialogFooter>
        <Button variant="ghost" disabled={pending} type="button" onClick={onCancel}>
          Anuluj
        </Button>
        <Button autoFocus onClick={onConfirm} disabled={!canSend}>
          {pending && <Loader2 className="h-4 w-4 animate-spin" />}
          <Send className="h-4 w-4" />
          Wyślij
        </Button>
      </DialogFooter>
    </DialogContent>
  );
}

function ThreadToolbar({ thread }: { thread: DmThread }) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [checkupOpen, setCheckupOpen] = useState(false);

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['thread', thread.id] });
    queryClient.invalidateQueries({ queryKey: ['threads'] });
    queryClient.invalidateQueries({ queryKey: ['checkups', thread.id] });
  };

  const flagMutation = useMutation({
    mutationFn: () => api.threads.setFlagged(thread.id, !thread.isFlagged),
    onSuccess: invalidate,
    onError: (e) => toast({ kind: 'error', title: 'Błąd', description: (e as Error).message }),
  });

  const statusMutation = useMutation({
    mutationFn: () =>
      api.threads.setStatus(thread.id, thread.status === 'done' ? 'inbox' : 'done'),
    onSuccess: () => {
      toast({
        kind: 'success',
        title: thread.status === 'done' ? 'Przywrócono z Done' : 'Przeniesiono do Done',
      });
      invalidate();
    },
    onError: (e) => toast({ kind: 'error', title: 'Błąd', description: (e as Error).message }),
  });

  return (
    <>
      <Button
        variant={thread.isFlagged ? 'default' : 'outline'}
        size="sm"
        onClick={() => flagMutation.mutate()}
        disabled={flagMutation.isPending}
        title={thread.isFlagged ? 'Zdejmij flagę' : 'Oznacz flagą'}
      >
        <Flag className="h-4 w-4" />
        {thread.isFlagged ? 'Oflagowane' : 'Flaga'}
      </Button>

      <Button
        variant={thread.pendingCheckupCount > 0 ? 'default' : 'outline'}
        size="sm"
        onClick={() => setCheckupOpen(true)}
        title="Zaplanuj follow-up"
      >
        <Clock3 className="h-4 w-4" />
        Check-up
        {thread.pendingCheckupCount > 0 && (
          <span className="ml-1 text-[10px] font-bold">({thread.pendingCheckupCount})</span>
        )}
      </Button>

      <Button
        variant="outline"
        size="sm"
        onClick={() => statusMutation.mutate()}
        disabled={statusMutation.isPending}
      >
        {thread.status === 'done' ? (
          <>
            <ArchiveRestore className="h-4 w-4" />
            Przywróć
          </>
        ) : (
          <>
            <Archive className="h-4 w-4" />
            Done
          </>
        )}
      </Button>

      <Dialog open={checkupOpen} onOpenChange={setCheckupOpen}>
        <CheckupDialog threadId={thread.id} onClose={() => setCheckupOpen(false)} />
      </Dialog>
    </>
  );
}

function CheckupDialog({ threadId, onClose }: { threadId: number; onClose: () => void }) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [note, setNote] = useState('');
  const [customDate, setCustomDate] = useState('');

  const checkupsQuery = useQuery({
    queryKey: ['checkups', threadId],
    queryFn: () => api.threads.listCheckups(threadId).then((r) => r.checkups),
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['checkups', threadId] });
    queryClient.invalidateQueries({ queryKey: ['thread', threadId] });
    queryClient.invalidateQueries({ queryKey: ['threads'] });
  };

  const createMutation = useMutation({
    mutationFn: (dueAt: Date) =>
      api.threads.createCheckup(threadId, {
        dueAt: dueAt.toISOString(),
        note: note.trim() || null,
      }),
    onSuccess: () => {
      setNote('');
      setCustomDate('');
      invalidate();
      toast({ kind: 'success', title: 'Check-up dodany' });
    },
    onError: (e) => toast({ kind: 'error', title: 'Błąd', description: (e as Error).message }),
  });

  const doneMutation = useMutation({
    mutationFn: (checkupId: number) => api.threads.markCheckupDone(threadId, checkupId),
    onSuccess: invalidate,
  });

  const deleteMutation = useMutation({
    mutationFn: (checkupId: number) => api.threads.deleteCheckup(threadId, checkupId),
    onSuccess: invalidate,
  });

  const addPreset = (days: number) => {
    const due = new Date(Date.now() + days * 24 * 60 * 60 * 1000);
    createMutation.mutate(due);
  };

  const addCustom = () => {
    if (!customDate) return;
    const due = new Date(customDate);
    if (Number.isNaN(due.getTime())) return;
    createMutation.mutate(due);
  };

  const pending = (checkupsQuery.data ?? []).filter((c) => c.doneAt === null);
  const done = (checkupsQuery.data ?? []).filter((c) => c.doneAt !== null);

  return (
    <DialogContent>
      <DialogHeader>
        <DialogTitle>Check-up — zaplanuj follow-up</DialogTitle>
        <DialogDescription>
          Wątek pojawi się w „Inbox" z badge'em DUE w dniu o który prosisz. Pending check-up'y
          automatycznie się odhaczają jak wyślesz wiadomość.
        </DialogDescription>
      </DialogHeader>

      <div className="flex flex-col gap-3">
        <div>
          <label className="text-xs font-bold uppercase tracking-wider text-foreground/50 mb-2 block">
            Notatka (opcjonalna)
          </label>
          <Textarea
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="np. spytać o postępy z modułem 2"
            rows={2}
          />
        </div>

        <div>
          <label className="text-xs font-bold uppercase tracking-wider text-foreground/50 mb-2 block">
            Za ile dni
          </label>
          <div className="flex gap-2 flex-wrap">
            {[2, 7, 14, 30].map((d) => (
              <Button
                key={d}
                variant="outline"
                size="sm"
                disabled={createMutation.isPending}
                onClick={() => addPreset(d)}
              >
                +{d}d
              </Button>
            ))}
            <input
              type="datetime-local"
              value={customDate}
              onChange={(e) => setCustomDate(e.target.value)}
              className="h-9 px-3 rounded-md border border-input bg-background/40 text-sm focus-visible:ring-2 focus-visible:ring-ring outline-none"
            />
            <Button
              variant="default"
              size="sm"
              onClick={addCustom}
              disabled={!customDate || createMutation.isPending}
            >
              Dodaj
            </Button>
          </div>
        </div>

        {pending.length > 0 && (
          <div>
            <h4 className="text-xs font-bold uppercase tracking-wider text-foreground/50 mb-2">
              Zaplanowane ({pending.length})
            </h4>
            <div className="flex flex-col gap-1.5">
              {pending.map((c) => (
                <CheckupRow
                  key={c.id}
                  due={c.dueAt}
                  note={c.note}
                  onDone={() => doneMutation.mutate(c.id)}
                  onDelete={() => deleteMutation.mutate(c.id)}
                  busy={doneMutation.isPending || deleteMutation.isPending}
                />
              ))}
            </div>
          </div>
        )}

        {done.length > 0 && (
          <details className="text-sm">
            <summary className="cursor-pointer text-foreground/50 hover:text-foreground">
              Historia ({done.length})
            </summary>
            <div className="flex flex-col gap-1 mt-2">
              {done.map((c) => (
                <div key={c.id} className="text-xs text-foreground/40 flex items-center gap-2">
                  <Check className="h-3 w-3 text-success" />
                  <span>{formatDateTime(c.dueAt)}</span>
                  {c.note && <span className="truncate">— {c.note}</span>}
                </div>
              ))}
            </div>
          </details>
        )}
      </div>

      <DialogFooter>
        <Button variant="ghost" onClick={onClose}>
          Zamknij
        </Button>
      </DialogFooter>
    </DialogContent>
  );
}

function CheckupRow({
  due,
  note,
  onDone,
  onDelete,
  busy,
}: {
  due: string;
  note: string | null;
  onDone: () => void;
  onDelete: () => void;
  busy: boolean;
}) {
  const dueDate = new Date(due);
  const overdue = dueDate.getTime() <= Date.now();
  return (
    <div
      className={cn(
        'flex items-center gap-2 p-2 rounded-md border',
        overdue ? 'border-primary/40 bg-primary/5' : 'border-border bg-card-hover/40',
      )}
    >
      <Clock3 className={cn('h-4 w-4 shrink-0', overdue ? 'text-primary' : 'text-foreground/50')} />
      <div className="flex-1 min-w-0">
        <div className={cn('text-sm font-medium', overdue && 'text-primary')}>
          {overdue ? 'DUE' : formatRelative(due)} · {formatDateTime(due)}
        </div>
        {note && <div className="text-xs text-foreground/60 truncate">{note}</div>}
      </div>
      <button
        type="button"
        onClick={onDone}
        disabled={busy}
        className="p-1 rounded hover:bg-success/10 text-foreground/40 hover:text-success"
        title="Odhacz"
      >
        <Check className="h-3.5 w-3.5" />
      </button>
      <button
        type="button"
        onClick={onDelete}
        disabled={busy}
        className="p-1 rounded hover:bg-destructive/10 text-foreground/40 hover:text-destructive"
        title="Usuń"
      >
        <Trash2 className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}
