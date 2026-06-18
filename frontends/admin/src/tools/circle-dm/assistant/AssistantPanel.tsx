import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  History,
  Loader2,
  MessageSquarePlus,
  Send,
  Sparkles,
  Square,
  X,
} from 'lucide-react';
import type { AssistantContext, AssistantMessage } from '@bfc/shared';
import { Button } from '@/core/components/ui/button';
import { Textarea } from '@/core/components/ui/textarea';
import { useToast } from '@/core/components/ui/toast';
import { useWsEvent } from '@/core/lib/ws';
import { api } from '@/tools/circle-dm/lib/api';
import { cn } from '@/core/lib/utils';
import { ActionProposalCard } from './ActionProposalCard';
import { useAssistant } from './AssistantContext';

/** Strip everything from the first ```action fence onward, even mid-stream. */
function visibleStreaming(text: string): string {
  const idx = text.indexOf('```action');
  if (idx === -1) return text;
  return text.slice(0, idx).trimEnd();
}

export function AssistantPanel() {
  const { isOpen, close, snapshot } = useAssistant();
  const queryClient = useQueryClient();
  const { toast } = useToast();

  // null = bieżąca (najnowsza); number = konkretna z historii
  const [activeConvId, setActiveConvId] = useState<number | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);

  const convQuery = useQuery({
    queryKey: ['assistant', 'conversation', activeConvId ?? 'current'],
    queryFn: () => api.assistant.getConversation(activeConvId ?? undefined),
    enabled: isOpen,
  });
  const listQuery = useQuery({
    queryKey: ['assistant', 'conversations'],
    queryFn: () => api.assistant.listConversations(),
    enabled: isOpen && historyOpen,
  });
  const conversationId = convQuery.data?.conversation.id ?? null;
  const messages = useMemo<AssistantMessage[]>(
    () => convQuery.data?.messages ?? [],
    [convQuery.data],
  );

  // Live streaming buffer for the assistant's in-flight reply.
  const [streamingText, setStreamingText] = useState('');
  const [streaming, setStreaming] = useState(false);

  useWsEvent('assistant:token', (event) => {
    if (event.conversationId !== conversationId) return;
    setStreamingText((s) => s + event.chunk);
  });
  useWsEvent('assistant:complete', (event) => {
    if (event.conversationId !== conversationId) return;
    setStreaming(false);
    setStreamingText('');
    queryClient.invalidateQueries({ queryKey: ['assistant'] });
  });
  useWsEvent('assistant:error', (event) => {
    if (event.conversationId !== conversationId) return;
    setStreaming(false);
    setStreamingText('');
    toast({ kind: 'error', title: 'Asystent', description: event.error });
  });

  // Auto-scroll on new content.
  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages.length, streamingText]);

  const [input, setInput] = useState('');
  const sendMutation = useMutation({
    mutationFn: ({ message, context }: { message: string; context: AssistantContext }) => {
      if (conversationId === null) throw new Error('No conversation');
      return api.assistant.turn(conversationId, message, context);
    },
    onMutate: () => {
      setStreaming(true);
      setStreamingText('');
    },
    onSuccess: () => {
      // The user-message persisted server-side; refetch to render it.
      queryClient.invalidateQueries({ queryKey: ['assistant', 'conversation'] });
    },
    onError: (err) => {
      setStreaming(false);
      toast({ kind: 'error', title: 'Wyślij', description: (err as Error).message });
    },
  });

  const onSend = () => {
    const text = input.trim();
    if (!text || streaming) return;
    setInput('');
    sendMutation.mutate({ message: text, context: snapshot });
  };

  const newMutation = useMutation({
    mutationFn: () => api.assistant.newConversation(),
    onSuccess: (res) => {
      setStreamingText('');
      setStreaming(false);
      // Switch the panel to the new conversation explicitly so the latest
      // becomes "active" without relying on the "current=latest" fallback.
      setActiveConvId(res.conversation.id);
      queryClient.invalidateQueries({ queryKey: ['assistant'] });
    },
  });

  const cancelMutation = useMutation({
    mutationFn: () => {
      if (conversationId === null) throw new Error('no conversation');
      return api.assistant.cancel(conversationId);
    },
    // Backend broadcasts assistant:complete after kill, which clears the
    // streaming flag - no need to do it here.
  });

  if (!isOpen) return null;

  return (
    <aside className="fixed top-16 right-0 bottom-0 w-full sm:w-[400px] z-30 border-l border-border bg-background flex flex-col">
      <header className="relative flex items-center gap-2 px-4 py-3 border-b border-border">
        <Sparkles className="h-5 w-5 text-primary fill-primary" />
        <div className="flex-1 min-w-0">
          <h2 className="font-semibold text-sm">BFC AI</h2>
          <p className="text-[11px] text-foreground/40 truncate">
            Kontekst: {humanizeKind(snapshot.kind)}
          </p>
        </div>
        <Button
          variant="ghost"
          size="sm"
          title="Historia rozmów"
          onClick={() => setHistoryOpen((v) => !v)}
          className={cn(historyOpen && 'bg-primary/15 text-primary')}
        >
          <History className="h-4 w-4" />
        </Button>
        <Button
          variant="ghost"
          size="sm"
          title="Nowa rozmowa"
          onClick={() => newMutation.mutate()}
          disabled={newMutation.isPending}
        >
          <MessageSquarePlus className="h-4 w-4" />
        </Button>
        <Button variant="ghost" size="sm" title="Zamknij (Cmd/Ctrl+J)" onClick={close}>
          <X className="h-4 w-4" />
        </Button>

        {historyOpen && (
          <>
            <div
              className="fixed inset-0 z-10"
              onClick={() => setHistoryOpen(false)}
              aria-hidden
            />
            <div className="absolute top-full left-3 right-3 mt-1 z-20 rounded-lg border border-border bg-card shadow-lg max-h-[60vh] overflow-y-auto">
              {listQuery.isLoading && (
                <div className="flex items-center justify-center py-6 text-foreground/40 text-xs">
                  <Loader2 className="h-4 w-4 animate-spin mr-2" /> Ładuję historię…
                </div>
              )}
              {listQuery.data && listQuery.data.conversations.length === 0 && (
                <div className="p-3 text-xs text-foreground/40 text-center">
                  Brak innych rozmów.
                </div>
              )}
              {listQuery.data?.conversations.map((c) => {
                const isCurrent = c.id === conversationId;
                return (
                  <button
                    key={c.id}
                    type="button"
                    className={cn(
                      'w-full text-left px-3 py-2 text-xs border-b border-border/40 last:border-0 hover:bg-foreground/5',
                      isCurrent && 'bg-primary/10',
                    )}
                    onClick={() => {
                      setActiveConvId(c.id);
                      setHistoryOpen(false);
                      setStreamingText('');
                      setStreaming(false);
                    }}
                  >
                    <div className="font-medium truncate">
                      {c.title || `Rozmowa #${c.id}`}
                    </div>
                    <div className="text-[10px] text-foreground/40">
                      {c.lastMessageAt
                        ? new Date(c.lastMessageAt).toLocaleString('pl-PL')
                        : 'pusta'}
                      {isCurrent && ' · bieżąca'}
                    </div>
                  </button>
                );
              })}
            </div>
          </>
        )}
      </header>

      <div ref={scrollRef} className="flex-1 overflow-y-auto px-3 py-3 flex flex-col gap-3">
        {convQuery.isLoading && (
          <div className="flex items-center justify-center py-12 text-foreground/40">
            <Loader2 className="h-4 w-4 animate-spin mr-2" />
            Ładuję rozmowę…
          </div>
        )}
        {!convQuery.isLoading && messages.length === 0 && !streaming && (
          <div className="text-xs text-foreground/40 py-8 text-center">
            Cześć. Mogę pomóc z draftami, personą, promptami, bazą wiedzy. Jak mnie poprosisz o
            edycję, zaproponuję zmianę z przyciskiem "Zastosuj".
          </div>
        )}
        {messages.map((m) => (
          <MessageBubble key={m.id} msg={m} />
        ))}
        {streaming && (
          <div className="flex flex-col gap-2">
            <div className="text-[11px] uppercase tracking-wider text-foreground/40">asystent</div>
            <div className="rounded-lg bg-card-hover/40 border border-border/50 p-3 text-sm whitespace-pre-wrap leading-relaxed">
              {visibleStreaming(streamingText)}
              <span className="inline-block w-1.5 h-3 ml-1 bg-primary animate-pulse align-middle" />
            </div>
          </div>
        )}
      </div>

      <div className="border-t border-border p-3 flex flex-col gap-2">
        <Textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              onSend();
            }
          }}
          placeholder={
            streaming
              ? 'Czekam aż skończy…'
              : 'Zapytaj o coś. Enter = wyślij, Shift+Enter = nowa linia.'
          }
          rows={3}
          disabled={streaming || conversationId === null}
          className="resize-none text-sm"
        />
        <div className="flex items-center justify-between">
          <span className="text-[11px] text-foreground/40">
            Kontekst: {humanizeKind(snapshot.kind)}
          </span>
          {streaming ? (
            <Button
              variant="outline"
              size="sm"
              onClick={() => cancelMutation.mutate()}
              disabled={cancelMutation.isPending || conversationId === null}
              title="Przerwij generowanie"
            >
              {cancelMutation.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Square className="h-4 w-4 fill-current" />
              )}
              Stop
            </Button>
          ) : (
            <Button
              variant="default"
              size="sm"
              onClick={onSend}
              disabled={!input.trim() || conversationId === null}
            >
              <Send className="h-4 w-4" />
              Wyślij
            </Button>
          )}
        </div>
      </div>
    </aside>
  );
}

function MessageBubble({ msg }: { msg: AssistantMessage }) {
  const isUser = msg.role === 'user';
  return (
    <div className="flex flex-col gap-1.5">
      <div className="text-[11px] uppercase tracking-wider text-foreground/40">
        {isUser ? 'Ty' : 'asystent'}
      </div>
      <div
        className={cn(
          'rounded-lg p-3 text-sm whitespace-pre-wrap leading-relaxed',
          isUser
            ? 'bg-primary/10 border border-primary/30'
            : 'bg-card-hover/40 border border-border/50',
        )}
      >
        {msg.content}
      </div>
      {!isUser && msg.actionProposal && <ActionProposalCard msg={msg} />}
    </div>
  );
}

function humanizeKind(kind: AssistantContext['kind']): string {
  switch (kind) {
    case 'thread':
      return 'wątek';
    case 'compose':
      return 'nowa wiadomość';
    case 'settings':
      return 'ustawienia';
    case 'account':
      return 'edycja konta';
    case 'inbox':
      return 'inbox';
    case 'none':
      return 'brak';
  }
}
