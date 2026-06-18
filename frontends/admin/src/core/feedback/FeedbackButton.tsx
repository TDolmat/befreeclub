import { useState } from 'react';
import { useLocation } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, HelpCircle, Loader2, Plus, RotateCcw, Trash2 } from 'lucide-react';
import type { FeedbackItem } from '@bfc/shared';
import { Button } from '@/core/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/core/components/ui/dialog';
import { Textarea } from '@/core/components/ui/textarea';
import { useToast } from '@/core/components/ui/toast';
import { useAuth } from '@/core/hooks/useAuth';
import { feedbackApi } from '@/core/lib/feedback-api';
import { cn } from '@/core/lib/utils';

function scopeFromPath(pathname: string): string {
  if (pathname.startsWith('/circle-dm')) return 'circle-dm';
  return 'general';
}

function isReviewer(email: string | null | undefined): boolean {
  return !!email && email.toLowerCase().includes('tomasz');
}

export function FeedbackButton() {
  const [open, setOpen] = useState(false);
  const auth = useAuth();
  const reviewer = isReviewer(auth.data?.email);

  // Badge: poll open count every 30s. Only renders for the reviewer (Tomasz).
  const countQuery = useQuery({
    queryKey: ['feedback', 'count'],
    queryFn: () => feedbackApi.count(),
    refetchInterval: 30_000,
    enabled: reviewer,
  });
  const openCount = countQuery.data?.openCount ?? 0;

  return (
    <>
      <Button
        variant="ghost"
        size="icon"
        title="Pomysły i problemy"
        onClick={() => setOpen(true)}
        className="relative"
      >
        <HelpCircle className="h-5 w-5" />
        {reviewer && openCount > 0 && (
          <span className="absolute -top-0.5 -right-0.5 min-w-[18px] h-[18px] px-1 rounded-full bg-destructive text-destructive-foreground text-[10px] font-bold flex items-center justify-center">
            {openCount > 99 ? '99+' : openCount}
          </span>
        )}
      </Button>
      <FeedbackDialog open={open} onClose={() => setOpen(false)} reviewer={reviewer} />
    </>
  );
}

function FeedbackDialog({
  open,
  onClose,
  reviewer,
}: {
  open: boolean;
  onClose: () => void;
  reviewer: boolean;
}) {
  const location = useLocation();
  const scope = scopeFromPath(location.pathname);
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [text, setText] = useState('');

  const listQuery = useQuery({
    queryKey: ['feedback', 'list'],
    queryFn: () => feedbackApi.list(),
    enabled: open,
  });

  const createMutation = useMutation({
    mutationFn: (body: string) => feedbackApi.create(body, scope),
    onSuccess: () => {
      setText('');
      queryClient.invalidateQueries({ queryKey: ['feedback'] });
    },
    onError: (err) =>
      toast({ kind: 'error', title: 'Nie zapisano', description: (err as Error).message }),
  });

  const statusMutation = useMutation({
    mutationFn: (v: { id: number; status: 'open' | 'done' }) =>
      feedbackApi.setStatus(v.id, v.status),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['feedback'] }),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => feedbackApi.remove(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['feedback'] }),
  });

  const submit = () => {
    const trimmed = text.trim();
    if (!trimmed || createMutation.isPending) return;
    createMutation.mutate(trimmed);
  };

  const items = listQuery.data?.items ?? [];
  const openItems = items.filter((i) => i.status === 'open');
  const doneItems = items.filter((i) => i.status === 'done');

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-2xl max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Pomysły i problemy</DialogTitle>
          <DialogDescription>
            Notuj co poprawić / dodać w apce.{' '}
            {reviewer
              ? 'Widzisz badge z licznikiem aktywnych spraw na ikonie.'
              : 'Tomasz przegląda i odhacza.'}
            <span className="block mt-1 text-[11px]">
              Aktualny scope: <span className="font-mono">{scope}</span>
            </span>
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-2">
          <Textarea
            autoFocus
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
            placeholder="Opisz pomysł albo problem. Enter = dodaj. Shift+Enter = nowa linia."
            rows={3}
            disabled={createMutation.isPending}
          />
          <div className="flex justify-end">
            <Button
              variant="default"
              size="sm"
              onClick={submit}
              disabled={!text.trim() || createMutation.isPending}
            >
              {createMutation.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Plus className="h-4 w-4" />
              )}
              Dodaj
            </Button>
          </div>
        </div>

        <div className="flex flex-col gap-2 mt-2">
          {listQuery.isLoading && (
            <div className="flex items-center justify-center py-6 text-foreground/40 text-sm">
              <Loader2 className="h-4 w-4 animate-spin mr-2" /> Ładuję…
            </div>
          )}
          {openItems.length === 0 && doneItems.length === 0 && !listQuery.isLoading && (
            <p className="text-xs text-foreground/40 text-center py-4">
              Brak zgłoszeń. Dorzuć pierwsze powyżej.
            </p>
          )}
          {openItems.length > 0 && (
            <>
              <div className="text-[11px] uppercase tracking-wider text-foreground/40 mt-2">
                Aktywne ({openItems.length})
              </div>
              {openItems.map((item) => (
                <FeedbackRow
                  key={item.id}
                  item={item}
                  onToggle={() =>
                    statusMutation.mutate({ id: item.id, status: 'done' })
                  }
                  onDelete={() => deleteMutation.mutate(item.id)}
                  busy={statusMutation.isPending || deleteMutation.isPending}
                />
              ))}
            </>
          )}
          {doneItems.length > 0 && (
            <>
              <div className="text-[11px] uppercase tracking-wider text-foreground/40 mt-3">
                Done ({doneItems.length})
              </div>
              {doneItems.map((item) => (
                <FeedbackRow
                  key={item.id}
                  item={item}
                  done
                  onToggle={() =>
                    statusMutation.mutate({ id: item.id, status: 'open' })
                  }
                  onDelete={() => deleteMutation.mutate(item.id)}
                  busy={statusMutation.isPending || deleteMutation.isPending}
                />
              ))}
            </>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

function FeedbackRow({
  item,
  done,
  onToggle,
  onDelete,
  busy,
}: {
  item: FeedbackItem;
  done?: boolean;
  onToggle: () => void;
  onDelete: () => void;
  busy: boolean;
}) {
  return (
    <div
      className={cn(
        'flex items-start gap-2 rounded-lg border border-border/60 px-3 py-2',
        done ? 'opacity-50 bg-card-hover/30' : 'bg-card-hover/60',
      )}
    >
      <button
        type="button"
        onClick={onToggle}
        disabled={busy}
        className={cn(
          'shrink-0 h-5 w-5 rounded border-2 grid place-items-center transition-colors mt-0.5',
          done ? 'border-success bg-success/20' : 'border-border hover:border-primary/60',
        )}
        title={done ? 'Cofnij' : 'Oznacz jako done'}
      >
        {done ? (
          <Check className="h-3 w-3 text-success" />
        ) : (
          <RotateCcw className="h-3 w-3 opacity-0 group-hover:opacity-40" />
        )}
      </button>
      <div className="flex-1 min-w-0">
        <p className="text-sm whitespace-pre-wrap break-words">{item.body}</p>
        <div className="text-[10px] text-foreground/40 mt-1">
          <span className="font-mono">{item.scope}</span>
          {' · '}
          {new Date(item.createdAt).toLocaleString('pl-PL')}
          {item.authorEmail && ` · ${item.authorEmail}`}
        </div>
      </div>
      <button
        type="button"
        onClick={onDelete}
        disabled={busy}
        className="text-foreground/40 hover:text-destructive shrink-0 p-1"
        title="Usuń"
      >
        <Trash2 className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}
