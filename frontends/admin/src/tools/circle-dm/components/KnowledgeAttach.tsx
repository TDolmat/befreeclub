import { useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Download, Eye, EyeOff, Loader2, Paperclip, Pencil, Plus, X } from 'lucide-react';
import type { KbScope } from '@bfc/shared';
import { Button } from '@/core/components/ui/button';
import { Input } from '@/core/components/ui/input';
import { Label } from '@/core/components/ui/label';
import { Textarea } from '@/core/components/ui/textarea';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/core/components/ui/dialog';
import { useToast } from '@/core/components/ui/toast';
import { api } from '@/tools/circle-dm/lib/api';
import { cn } from '@/core/lib/utils';

function fmtTok(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

/** Small ring that fills clockwise, percentage next to it, tooltip on hover. */
function CapacityRing({
  total,
  budget,
  hardCeiling,
  globalTokens,
  docCount,
  scope,
}: {
  total: number;
  budget: number;
  hardCeiling: number;
  globalTokens: number;
  docCount: number;
  scope: KbScope;
}) {
  const pct = Math.min(100, Math.round((total / budget) * 100));
  const over = total > budget;
  const R = 9;
  const C = 2 * Math.PI * R;
  const offset = C * (1 - Math.min(100, pct) / 100);

  return (
    <div className="relative group flex items-center gap-1.5">
      <svg width="24" height="24" viewBox="0 0 24 24" className="-rotate-90">
        <circle cx="12" cy="12" r={R} fill="none" strokeWidth="3" className="stroke-border/60" />
        <circle
          cx="12"
          cy="12"
          r={R}
          fill="none"
          strokeWidth="3"
          strokeLinecap="round"
          strokeDasharray={C}
          strokeDashoffset={offset}
          className={cn('transition-all', over ? 'stroke-warning' : 'stroke-primary')}
        />
      </svg>
      <span
        className={cn('text-xs tabular-nums', over ? 'text-warning' : 'text-foreground/50')}
      >
        {pct}%
      </span>
      <div
        className="pointer-events-none absolute left-0 bottom-full mb-2 z-50 w-64 rounded-lg border border-border bg-card p-3 text-xs leading-relaxed shadow-lg opacity-0 group-hover:opacity-100 transition-opacity"
      >
        <div className="font-semibold mb-1">Budżet kontekstu bazy wiedzy</div>
        <div className="text-foreground/70">
          ~{fmtTok(total)} / {fmtTok(budget)} tok wykorzystane ({pct}%)
        </div>
        <div className="text-foreground/50">
          Twardy limit: {fmtTok(hardCeiling)} tok (powyżej blok jest obcinany).
        </div>
        <div className="text-foreground/50 mt-1">
          {docCount} {docCount === 1 ? 'dokument' : 'dokumentów'} włączonych
          {scope === 'account' && globalTokens > 0
            ? `, w tym ~${fmtTok(globalTokens)} tok z bazy globalnej`
            : ''}
          .
        </div>
        {over && (
          <div className="text-warning mt-1">Ponad budżet — to spowalnia generowanie.</div>
        )}
      </div>
    </div>
  );
}

export function KnowledgeAttach({
  scope,
  accountId,
}: {
  scope: KbScope;
  accountId?: number;
}) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const fileRef = useRef<HTMLInputElement>(null);
  const [adding, setAdding] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [deletingId, setDeletingId] = useState<number | null>(null);

  const kbQuery = useQuery({
    queryKey: ['kb', scope, accountId ?? null],
    queryFn: () => api.kb.list(scope, accountId),
  });
  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ['kb', scope, accountId ?? null] });

  const uploadMutation = useMutation({
    mutationFn: (file: File) =>
      api.kb.upload(scope, file, scope === 'account' ? { adminAccountId: accountId } : {}),
    onSuccess: () => {
      invalidate();
      toast({ kind: 'success', title: 'Plik dołączony' });
    },
    onError: (err) =>
      toast({
        kind: 'error',
        title: 'Nie udało się wczytać pliku',
        description: (err as Error).message,
      }),
  });

  const toggleMutation = useMutation({
    mutationFn: (v: { id: number; enabled: boolean }) =>
      api.kb.update(v.id, { enabled: v.enabled }),
    onSuccess: invalidate,
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.kb.remove(id),
    onSuccess: () => {
      setDeletingId(null);
      invalidate();
    },
  });

  const cap = kbQuery.data?.capacity;
  const docs = kbQuery.data?.documents ?? [];

  return (
    <div className="flex flex-col gap-2">
      <input
        ref={fileRef}
        type="file"
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) uploadMutation.mutate(f);
          e.target.value = '';
        }}
      />

      <div className="flex items-center gap-2 flex-wrap">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-8 px-2 text-foreground/70"
          onClick={() => fileRef.current?.click()}
          disabled={uploadMutation.isPending}
        >
          {uploadMutation.isPending ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Paperclip className="h-4 w-4" />
          )}
          Załącz plik
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-8 px-2 text-foreground/70"
          onClick={() => setAdding(true)}
        >
          <Plus className="h-4 w-4" />
          Wklej tekst
        </Button>
        <div className="flex-1" />
        {cap && (docs.length > 0 || cap.totalTokens > 0) && (
          <CapacityRing
            total={cap.totalTokens}
            budget={cap.budget}
            hardCeiling={cap.hardCeiling}
            globalTokens={cap.globalTokens}
            docCount={docs.filter((d) => d.enabled).length}
            scope={scope}
          />
        )}
      </div>

      {docs.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {docs.map((d) => (
            <span
              key={d.id}
              className={cn(
                'inline-flex items-center gap-1.5 rounded-full border border-border/60 bg-card-hover/40 pl-1.5 pr-1 py-1 text-xs',
                !d.enabled && 'opacity-45',
              )}
            >
              <button
                type="button"
                title={d.enabled ? 'Wyłącz (nie trafia do AI)' : 'Włącz'}
                className="text-foreground/50 hover:text-foreground"
                onClick={() => toggleMutation.mutate({ id: d.id, enabled: !d.enabled })}
              >
                {d.enabled ? <Eye className="h-3.5 w-3.5" /> : <EyeOff className="h-3.5 w-3.5" />}
              </button>
              <span className="max-w-[200px] truncate font-medium" title={d.title}>
                {d.title}
              </span>
              <span className="text-foreground/35">~{fmtTok(d.tokenEstimate)}t</span>
              {d.hasOriginal && (
                <a
                  href={api.kb.originalUrl(d.id)}
                  title="Pobierz oryginał"
                  className="text-foreground/40 hover:text-foreground"
                >
                  <Download className="h-3.5 w-3.5" />
                </a>
              )}
              <button
                type="button"
                title="Edytuj treść"
                className="text-foreground/40 hover:text-foreground"
                onClick={() => setEditingId(d.id)}
              >
                <Pencil className="h-3.5 w-3.5" />
              </button>
              <button
                type="button"
                title="Usuń"
                className="rounded-full p-0.5 text-foreground/40 hover:bg-destructive/15 hover:text-destructive"
                onClick={() => setDeletingId(d.id)}
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </span>
          ))}
        </div>
      )}

      <AddTextDialog
        open={adding}
        onClose={() => setAdding(false)}
        scope={scope}
        accountId={accountId}
        onSaved={invalidate}
      />
      <EditDialog id={editingId} onClose={() => setEditingId(null)} onSaved={invalidate} />

      <Dialog open={deletingId !== null} onOpenChange={(o) => !o && setDeletingId(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Usunąć dokument?</DialogTitle>
            <DialogDescription>
              Zniknie z bazy wiedzy i przestanie trafiać do generowanych wiadomości. Oryginalny
              plik też zostanie skasowany.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setDeletingId(null)}>
              Anuluj
            </Button>
            <Button
              variant="destructive"
              onClick={() => deletingId !== null && deleteMutation.mutate(deletingId)}
              disabled={deleteMutation.isPending}
            >
              {deleteMutation.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
              Usuń
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function AddTextDialog({
  open,
  onClose,
  scope,
  accountId,
  onSaved,
}: {
  open: boolean;
  onClose: () => void;
  scope: KbScope;
  accountId?: number;
  onSaved: () => void;
}) {
  const { toast } = useToast();
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');

  const mutation = useMutation({
    mutationFn: () =>
      api.kb.createManual({
        scope,
        adminAccountId: scope === 'account' ? (accountId ?? null) : null,
        title: title.trim(),
        bodyText: body,
      }),
    onSuccess: () => {
      setTitle('');
      setBody('');
      onSaved();
      onClose();
      toast({ kind: 'success', title: 'Dodano do bazy wiedzy' });
    },
    onError: (err) =>
      toast({
        kind: 'error',
        title: 'Nie udało się zapisać',
        description: (err as Error).message,
      }),
  });

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Wklej tekst do bazy wiedzy</DialogTitle>
          <DialogDescription>
            Kontekst marki, zasady stylu albo przykłady wiadomości. Trafia jako materiał
            referencyjny do każdej generowanej i formatowanej wiadomości.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <div>
            <Label htmlFor="kb-title">Tytuł</Label>
            <Input
              id="kb-title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="np. Styl wiadomości Krystiana / Kontekst marki BFC"
            />
          </div>
          <div>
            <Label htmlFor="kb-body">Treść</Label>
            <Textarea
              id="kb-body"
              rows={12}
              value={body}
              onChange={(e) => setBody(e.target.value)}
              className="font-mono text-xs"
            />
          </div>
        </div>
        <DialogFooter className="mt-4">
          <Button variant="ghost" onClick={onClose}>
            Anuluj
          </Button>
          <Button
            onClick={() => mutation.mutate()}
            disabled={mutation.isPending || !title.trim() || !body.trim()}
          >
            {mutation.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
            Dodaj
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function EditDialog({
  id,
  onClose,
  onSaved,
}: {
  id: number | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { toast } = useToast();
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');

  useQuery({
    queryKey: ['kb-doc', id],
    queryFn: async () => {
      const doc = await api.kb.get(id!);
      setTitle(doc.title);
      setBody(doc.bodyText);
      return doc;
    },
    enabled: id !== null,
  });

  const mutation = useMutation({
    mutationFn: () => api.kb.update(id!, { title: title.trim(), bodyText: body }),
    onSuccess: () => {
      onSaved();
      onClose();
      toast({ kind: 'success', title: 'Zapisano' });
    },
    onError: (err) =>
      toast({
        kind: 'error',
        title: 'Nie udało się zapisać',
        description: (err as Error).message,
      }),
  });

  return (
    <Dialog open={id !== null} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Edytuj dokument</DialogTitle>
          <DialogDescription>
            Zmieniasz tekst który trafia do AI. Oryginalny plik (jeśli był) zostaje bez zmian.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <div>
            <Label htmlFor="kb-edit-title">Tytuł</Label>
            <Input
              id="kb-edit-title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
          </div>
          <div>
            <Label htmlFor="kb-edit-body">Treść</Label>
            <Textarea
              id="kb-edit-body"
              rows={14}
              value={body}
              onChange={(e) => setBody(e.target.value)}
              className="font-mono text-xs"
            />
          </div>
        </div>
        <DialogFooter className="mt-4">
          <Button variant="ghost" onClick={onClose}>
            Anuluj
          </Button>
          <Button
            onClick={() => mutation.mutate()}
            disabled={mutation.isPending || !title.trim() || !body.trim()}
          >
            {mutation.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
            Zapisz
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
