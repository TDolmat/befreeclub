import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useMutation } from '@tanstack/react-query';
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  Loader2,
  Send,
  Sparkles,
  Wand2,
  X,
  XCircle,
} from 'lucide-react';
import { Button } from '@/core/components/ui/button';
import { Card, CardContent } from '@/core/components/ui/card';
import { Textarea } from '@/core/components/ui/textarea';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/core/components/ui/dialog';
import { Avatar } from '@/core/components/Avatar';
import { useToast } from '@/core/components/ui/toast';
import { api } from '@/tools/circle-dm/lib/api';
import {
  type BulkQueue,
  type BulkQueueItem,
  clearBulkQueue,
  getBulkQueue,
  setBulkQueue,
} from '@/tools/circle-dm/lib/bulk-queue';
import { formatRelative } from '@/core/lib/format';

type BulkResult = {
  totalCount: number;
  okCount: number;
  results: Array<{
    kind: 'thread' | 'member';
    threadId: number | null;
    memberId: number | null;
    ok: boolean;
    error?: string;
  }>;
};

function itemKey(item: BulkQueueItem): string {
  return item.kind === 'thread' ? `t:${item.threadId}` : `m:${item.memberId}`;
}

export function BulkComposePage() {
  const navigate = useNavigate();
  const [queue, setQueue] = useState<BulkQueue | null>(null);
  const [text, setText] = useState('');
  const [busy, setBusy] = useState<null | 'format' | 'send'>(null);
  const [confirming, setConfirming] = useState(false);
  const [results, setResults] = useState<BulkResult | null>(null);
  const { toast } = useToast();

  useEffect(() => {
    setQueue(getBulkQueue());
  }, []);

  const removeItem = (key: string) => {
    if (!queue) return;
    const next: BulkQueue = {
      ...queue,
      items: queue.items.filter((it) => itemKey(it) !== key),
    };
    setQueue(next);
    setBulkQueue(next);
  };

  const formatMutation = useMutation({
    mutationFn: () => {
      if (!queue) throw new Error('Brak kolejki');
      setBusy('format');
      return api.format.bulk(queue.adminAccountId, text);
    },
    onSuccess: (r) => {
      setText(r.text);
      toast({ kind: 'info', title: 'Sformatowano' });
    },
    onError: (err) =>
      toast({ kind: 'error', title: 'Błąd formatowania', description: (err as Error).message }),
    onSettled: () => setBusy(null),
  });

  const sendMutation = useMutation({
    mutationFn: () => {
      if (!queue) throw new Error('Brak kolejki');
      setBusy('send');
      const items = queue.items.map((it) =>
        it.kind === 'thread'
          ? { kind: 'thread' as const, threadId: it.threadId }
          : {
              kind: 'member' as const,
              adminAccountId: queue.adminAccountId,
              memberId: it.memberId,
            },
      );
      return api.bulk.send(items, text);
    },
    onSuccess: (r) => {
      setResults(r);
      setConfirming(false);
      if (r.okCount === r.totalCount) {
        toast({
          kind: 'success',
          title: 'Wysłano wszystkim',
          description: `${r.okCount}/${r.totalCount}`,
        });
      } else {
        toast({
          kind: 'warning',
          title: 'Częściowo wysłano',
          description: `${r.okCount}/${r.totalCount} OK`,
        });
      }
    },
    onError: (err) => {
      setConfirming(false);
      toast({ kind: 'error', title: 'Błąd wysyłki', description: (err as Error).message });
    },
    onSettled: () => setBusy(null),
  });

  const canSend = useMemo(
    () => text.trim().length > 0 && queue !== null && queue.items.length > 0 && busy === null,
    [text, queue, busy],
  );

  if (!queue) {
    return (
      <div className="max-w-md mx-auto py-12 animate-fade-in">
        <Card>
          <CardContent className="py-10 text-center">
            <AlertTriangle className="h-8 w-8 text-foreground/40 mx-auto mb-3" />
            <p className="text-foreground/70 mb-4">
              Brak zaznaczonych odbiorców. Wróć do inbox'a i zaznacz osoby do których chcesz napisać.
            </p>
            <Link to="/circle-dm">
              <Button variant="outline" size="sm">
                <ArrowLeft className="h-4 w-4" />
                Wróć do inbox'a
              </Button>
            </Link>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (results) {
    return (
      <BulkResultsView
        results={results}
        items={queue.items}
        onDone={() => {
          clearBulkQueue();
          navigate('/circle-dm');
        }}
      />
    );
  }

  const total = queue.items.length;
  const threadCount = queue.items.filter((it) => it.kind === 'thread').length;
  const memberCount = total - threadCount;

  return (
    <div className="animate-fade-in">
      <div className="flex items-center gap-3 mb-5">
        <Link to="/circle-dm">
          <Button variant="ghost" size="sm">
            <ArrowLeft className="h-4 w-4" />
            Inbox
          </Button>
        </Link>
        <h1 className="auth-title text-2xl flex items-center gap-3">
          <Send className="h-6 w-6 text-primary" />
          Wiadomość do wielu osób
        </h1>
      </div>

      <p className="text-sm text-foreground/60 mb-5">
        Ten sam tekst pójdzie do {total} {total === 1 ? 'osoby' : 'osób'}
        {memberCount > 0 && (
          <span className="text-foreground/40">
            {' '}
            ({threadCount} z istniejących wątków, {memberCount} nowych)
          </span>
        )}
        {' — '}
        wysyłka indywidualna per osoba (nie group chat). Pamiętaj że to NIE jest personalizowane —
        wszyscy dostaną dokładnie tę samą treść.
      </p>

      <div className="grid lg:grid-cols-[minmax(280px,380px)_1fr] gap-4">
        <Card>
          <CardContent className="p-4 flex flex-col gap-2">
            <div className="flex items-center justify-between mb-1">
              <h3 className="text-xs font-bold uppercase tracking-wider text-foreground/50">
                Odbiorcy ({total})
              </h3>
            </div>
            <div className="flex flex-col gap-1 max-h-[60vh] overflow-y-auto pr-1">
              {queue.items.map((it) => {
                const key = itemKey(it);
                return (
                  <div
                    key={key}
                    className="flex items-center gap-2.5 p-2 rounded-md hover:bg-card-hover group"
                  >
                    <Avatar name={it.name} url={it.avatarUrl} size="sm" />
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-medium truncate flex items-center gap-1.5">
                        {it.name}
                        {it.kind === 'member' && (
                          <span className="badge-info text-[9px] uppercase tracking-wider px-1.5 py-0 rounded">
                            nowy
                          </span>
                        )}
                      </div>
                      {it.lastMessagePreview && (
                        <div className="text-[11px] text-foreground/40 truncate">
                          {it.lastMessageAt && `${formatRelative(it.lastMessageAt)} · `}
                          {it.lastMessagePreview}
                        </div>
                      )}
                    </div>
                    <button
                      type="button"
                      onClick={() => removeItem(key)}
                      className="opacity-0 group-hover:opacity-100 transition-opacity p-1 rounded hover:bg-destructive/10 text-foreground/40 hover:text-destructive"
                      title="Usuń z kolejki"
                    >
                      <X className="h-3.5 w-3.5" />
                    </button>
                  </div>
                );
              })}
            </div>
            {total === 0 && (
              <div className="text-center text-foreground/50 text-sm py-4">
                Wszyscy usunięci.{' '}
                <Link to="/circle-dm" className="text-info underline">
                  Wróć
                </Link>
                .
              </div>
            )}
          </CardContent>
        </Card>

        <Card glow>
          <CardContent className="p-4 flex flex-col gap-3">
            <h3 className="auth-title text-base flex items-center gap-2">
              <Sparkles className="h-4 w-4 text-primary" />
              Wiadomość
            </h3>
            <Textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder={
                busy === 'format'
                  ? 'Formatuję…'
                  : 'Wpisz treść lub brain dump.\n\nUWAGA: tekst pójdzie identyczny do wszystkich zaznaczonych — bez personalizacji per osoba. „Formatuj z AI" przerobi to neutralnie.'
              }
              rows={14}
              disabled={busy === 'format'}
              className="font-sans text-sm leading-relaxed"
            />
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => formatMutation.mutate()}
                disabled={busy !== null || !text.trim()}
              >
                {busy === 'format' ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Wand2 className="h-4 w-4" />
                )}
                Formatuj z AI
              </Button>
              <Button
                variant="default"
                size="sm"
                disabled={!canSend}
                onClick={() => setConfirming(true)}
              >
                <Send className="h-4 w-4" />
                Wyślij do wszystkich ({total})
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>

      <Dialog open={confirming} onOpenChange={setConfirming}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              Wysłać do {total} {total === 1 ? 'osoby' : 'osób'}?
            </DialogTitle>
            <DialogDescription>
              Każdy dostanie indywidualnie tę samą wiadomość. Wysyłka sekwencyjna, może potrwać
              chwilę.
              {memberCount > 0 && (
                <>
                  {' '}
                  Dla {memberCount} {memberCount === 1 ? 'osoby' : 'osób'} bez istniejącego wątku
                  zostanie założony nowy DM.
                </>
              )}
            </DialogDescription>
          </DialogHeader>
          <div className="rounded-md border border-primary/30 bg-card-hover/50 p-3 max-h-72 overflow-y-auto whitespace-pre-wrap text-sm">
            {text}
          </div>
          <DialogFooter>
            <Button variant="ghost" type="button" onClick={() => setConfirming(false)}>
              Anuluj
            </Button>
            <Button onClick={() => sendMutation.mutate()} disabled={busy === 'send'}>
              {busy === 'send' && <Loader2 className="h-4 w-4 animate-spin" />}
              <Send className="h-4 w-4" />
              Wyślij wszystkim
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function BulkResultsView({
  results,
  items,
  onDone,
}: {
  results: BulkResult;
  items: BulkQueueItem[];
  onDone: () => void;
}) {
  const byKey = new Map(items.map((it) => [itemKey(it), it]));
  const allOk = results.okCount === results.totalCount;
  return (
    <div className="max-w-2xl mx-auto animate-fade-in">
      <Card glow={allOk}>
        <CardContent className="p-5">
          <div className="flex items-center gap-3 mb-4">
            {allOk ? (
              <CheckCircle2 className="h-8 w-8 text-success" />
            ) : (
              <AlertTriangle className="h-8 w-8 text-warning" />
            )}
            <div>
              <h2 className="auth-title text-xl">
                {allOk ? 'Wszystko wysłane' : 'Wysłano częściowo'}
              </h2>
              <p className="text-sm text-foreground/60">
                {results.okCount} z {results.totalCount}{' '}
                {results.totalCount === 1 ? 'osoby' : 'osób'}
              </p>
            </div>
          </div>
          <div className="flex flex-col gap-1 max-h-[50vh] overflow-y-auto">
            {results.results.map((r, idx) => {
              const lookupKey =
                r.kind === 'thread' && r.threadId !== null
                  ? `t:${r.threadId}`
                  : r.kind === 'member' && r.memberId !== null
                    ? `m:${r.memberId}`
                    : '';
              const item = byKey.get(lookupKey);
              return (
                <div
                  key={`${r.kind}-${idx}`}
                  className={`flex items-center gap-3 p-2 rounded-md border ${
                    r.ok ? 'border-success/20 bg-success/5' : 'border-destructive/30 bg-destructive/5'
                  }`}
                >
                  {r.ok ? (
                    <CheckCircle2 className="h-4 w-4 text-success shrink-0" />
                  ) : (
                    <XCircle className="h-4 w-4 text-destructive shrink-0" />
                  )}
                  <span className="font-medium text-sm flex-1 min-w-0 truncate">
                    {item?.name ?? (r.kind === 'thread' ? `Thread ${r.threadId}` : `Member ${r.memberId}`)}
                  </span>
                  {!r.ok && r.error && (
                    <span className="text-xs text-destructive truncate max-w-[40%]" title={r.error}>
                      {r.error}
                    </span>
                  )}
                </div>
              );
            })}
          </div>
          <div className="mt-5 flex justify-end">
            <Button variant="default" onClick={onDone}>
              Wróć do inbox'a
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
