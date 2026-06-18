import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import {
  CheckCircle2,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  Trash2,
} from 'lucide-react';
import {
  type AdminAccount,
  type CreateAdminAccount,
  createAdminAccountSchema,
} from '@bfc/shared';
import { Button } from '@/core/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/core/components/ui/card';
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
import { setActiveAccountId } from '@/tools/circle-dm/lib/account-context';
import { useAccounts, useActiveAccountId } from '@/tools/circle-dm/hooks/useAccounts';
import { KnowledgeAttach } from '@/tools/circle-dm/components/KnowledgeAttach';
import { useRegisterAssistantContext } from '@/tools/circle-dm/assistant/AssistantContext';
import { cn } from '@/core/lib/utils';

const DEFAULT_PERSONA = `Jesteś sprawnie piszącym współzałożycielem klubu Be Free Club.
- Piszesz po polsku, mówionym tonem, w pierwszej osobie.
- Krótko i naturalnie. Nie korpomowa, nie chatbotowy "rozumiem, że...".
- Bez pompatycznego "z chęcią", bez emoji w UI, bez wykrzykników na siłę.
- Pomagasz, ale stawiasz granice. Pisze człowiek do człowieka.`;

export function AccountsPage() {
  const { data: accounts, isLoading } = useAccounts();
  const activeId = useActiveAccountId();
  const [adding, setAdding] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const editingAccount = accounts?.find((a) => a.id === editingId) ?? null;

  const testMutation = useMutation({
    mutationFn: (id: number) => api.accounts.testConnection(id),
    onSuccess: (data) => {
      if (data.ok) {
        toast({
          kind: 'success',
          title: 'Token działa',
          description: `community_id=${data.communityId}, member_id=${data.communityMemberId}`,
        });
        queryClient.invalidateQueries({ queryKey: ['accounts'] });
      } else {
        toast({ kind: 'error', title: 'Test nie przeszedł', description: data.error });
      }
    },
    onError: (err) => toast({ kind: 'error', title: 'Błąd', description: (err as Error).message }),
  });

  const syncMutation = useMutation({
    mutationFn: (id: number) => api.accounts.sync(id),
    onSuccess: (data) => {
      toast({
        kind: 'success',
        title: 'Sync OK',
        description: `${data.changedThreadIds.length} wątków odświeżonych`,
      });
      queryClient.invalidateQueries({ queryKey: ['threads'] });
    },
    onError: (err) => toast({ kind: 'error', title: 'Sync error', description: (err as Error).message }),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.accounts.remove(id),
    onSuccess: () => {
      setDeletingId(null);
      queryClient.invalidateQueries({ queryKey: ['accounts'] });
      toast({ kind: 'success', title: 'Konto usunięte' });
    },
  });

  return (
    <div className="max-w-3xl mx-auto animate-fade-in">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="auth-title text-2xl mb-1">Konta admina</h1>
          <p className="text-sm text-foreground/60">
            Każde konto = jedna tożsamość admina w Circle (etykieta, email, persona, modele).
            Prompty globalne (style + formatowanie) ustawiasz w Ustawieniach.
          </p>
        </div>
        <Button onClick={() => setAdding(true)} variant="outline">
          <Plus className="h-4 w-4" />
          Dodaj konto
        </Button>
      </div>

      {isLoading && (
        <div className="flex items-center justify-center py-16 text-foreground/50">
          <Loader2 className="h-5 w-5 animate-spin mr-2" />
          Ładuję konta…
        </div>
      )}

      {!isLoading && accounts && accounts.length === 0 && (
        <Card glow className="animate-scale-in">
          <CardContent className="py-10 text-center">
            <p className="text-foreground/70 mb-4">
              Nie ma jeszcze żadnego konta. Dodaj pierwsze, żeby zacząć zarządzać DMami.
            </p>
            <Button onClick={() => setAdding(true)}>
              <Plus className="h-4 w-4" />
              Dodaj pierwsze konto
            </Button>
          </CardContent>
        </Card>
      )}

      <div className="flex flex-col gap-4">
        {accounts?.map((a) => {
          const isActive = a.id === activeId;
          return (
            <Card
              key={a.id}
              glow={isActive}
              className={cn('animate-fade-in transition-colors', isActive && 'border-primary/40')}
            >
              <CardHeader>
                <div className="flex items-start justify-between gap-3">
                  <div className="flex-1 min-w-0">
                    <CardTitle className="flex items-center gap-2">
                      {a.label}
                      {!a.isActive && (
                        <span className="badge-error text-[10px] uppercase tracking-wider px-2 py-0.5 rounded-full">
                          inactive
                        </span>
                      )}
                    </CardTitle>
                    <CardDescription className="mt-1">{a.email}</CardDescription>
                    <div className="mt-2 flex flex-wrap gap-2 text-xs">
                      {a.communityId !== null && (
                        <span className="badge-brand px-2 py-0.5 rounded-full">
                          community #{a.communityId}
                        </span>
                      )}
                      {a.communityMemberId !== null && (
                        <span className="badge-brand px-2 py-0.5 rounded-full">
                          member #{a.communityMemberId}
                        </span>
                      )}
                      {a.lastSyncedAt && (
                        <span className="badge-info px-2 py-0.5 rounded-full">
                          sync: {new Date(a.lastSyncedAt).toLocaleTimeString('pl-PL')}
                        </span>
                      )}
                    </div>
                  </div>
                  <div className="flex gap-1.5 shrink-0">
                    <Button
                      variant={isActive ? 'default' : 'outline'}
                      size="sm"
                      onClick={() => setActiveAccountId(a.id)}
                    >
                      {isActive ? 'Aktywne' : 'Aktywuj'}
                    </Button>
                  </div>
                </div>
              </CardHeader>
              <CardContent className="flex flex-wrap items-center gap-2">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => testMutation.mutate(a.id)}
                  disabled={testMutation.isPending}
                >
                  {testMutation.isPending && testMutation.variables === a.id ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <CheckCircle2 className="h-4 w-4 text-success" />
                  )}
                  Test połączenia
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => syncMutation.mutate(a.id)}
                  disabled={syncMutation.isPending}
                >
                  {syncMutation.isPending && syncMutation.variables === a.id ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <RefreshCw className="h-4 w-4" />
                  )}
                  Synchronizuj
                </Button>
                <Button variant="ghost" size="sm" onClick={() => setEditingId(a.id)}>
                  <Pencil className="h-4 w-4" />
                  Edytuj
                </Button>
                <div className="flex-1" />
                <Button
                  variant="ghost"
                  size="sm"
                  className="text-destructive hover:text-destructive hover:bg-destructive/10"
                  onClick={() => setDeletingId(a.id)}
                >
                  <Trash2 className="h-4 w-4" />
                  Usuń
                </Button>
              </CardContent>
            </Card>
          );
        })}
      </div>

      <AddAccountDialog open={adding} onOpenChange={setAdding} />

      <EditAccountDialog
        account={editingAccount}
        onClose={() => setEditingId(null)}
      />

      <Dialog open={deletingId !== null} onOpenChange={(o) => !o && setDeletingId(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Usunąć konto?</DialogTitle>
            <DialogDescription>
              Wszystkie wątki, wiadomości, drafty i audyt wysyłek powiązane z tym kontem zostaną
              usunięte z lokalnej bazy. W Circle nic się nie dzieje.
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
              Usuń bezpowrotnie
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function AccountForm({
  initial,
  submitLabel,
  pending,
  kbAccountId,
  onSubmit,
  onCancel,
}: {
  initial?: Partial<CreateAdminAccount> & { isActive?: boolean };
  submitLabel: string;
  pending: boolean;
  /** Set in edit mode → shows the per-account knowledge attach under persona. */
  kbAccountId?: number;
  onSubmit: (v: CreateAdminAccount) => void;
  onCancel: () => void;
}) {
  const form = useForm<CreateAdminAccount>({
    resolver: zodResolver(createAdminAccountSchema),
    defaultValues: {
      label: initial?.label ?? '',
      email: initial?.email ?? '',
      systemPrompt: initial?.systemPrompt ?? DEFAULT_PERSONA,
    },
  });

  // Re-seed form when `initial` changes (switching from one account to another in edit)
  useEffect(() => {
    if (initial) {
      form.reset({
        label: initial.label ?? '',
        email: initial.email ?? '',
        systemPrompt: initial.systemPrompt ?? DEFAULT_PERSONA,
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initial?.label, initial?.email, initial?.systemPrompt]);

  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={form.handleSubmit((v) => onSubmit(v))}
    >
      <div>
        <Label htmlFor="label">Etykieta</Label>
        <Input id="label" placeholder="Tomasz @ befreeclub" {...form.register('label')} />
        {form.formState.errors.label && (
          <p className="text-xs text-destructive mt-1">{form.formState.errors.label.message}</p>
        )}
      </div>
      <div>
        <Label htmlFor="email">Email (logowania do Circle)</Label>
        <Input id="email" type="email" {...form.register('email')} />
        {form.formState.errors.email && (
          <p className="text-xs text-destructive mt-1">{form.formState.errors.email.message}</p>
        )}
      </div>
      <div>
        <Label htmlFor="prompt">System prompt (persona)</Label>
        <Textarea
          id="prompt"
          rows={8}
          {...form.register('systemPrompt')}
          className="font-mono text-xs"
        />
        {form.formState.errors.systemPrompt && (
          <p className="text-xs text-destructive mt-1">
            {form.formState.errors.systemPrompt.message}
          </p>
        )}
        {kbAccountId ? (
          <div className="mt-2 border-t border-border/50 pt-2">
            <p className="text-[11px] text-foreground/40 mb-1.5">
              Baza wiedzy tej persony — np. jak konkretnie ta osoba pisze, jej przykłady
              wiadomości. Doklejane do bazy globalnej przy generowaniu z tego konta.
            </p>
            <KnowledgeAttach scope="account" accountId={kbAccountId} />
          </div>
        ) : (
          <p className="text-[11px] text-foreground/40 mt-1.5">
            Pliki bazy wiedzy tej persony dołączysz po zapisaniu konta (Edytuj).
          </p>
        )}
      </div>

      <DialogFooter className="mt-4">
        <Button variant="ghost" type="button" onClick={onCancel}>
          Anuluj
        </Button>
        <Button type="submit" disabled={pending}>
          {pending && <Loader2 className="h-4 w-4 animate-spin" />}
          {submitLabel}
        </Button>
      </DialogFooter>
    </form>
  );
}

function AddAccountDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const createMutation = useMutation({
    mutationFn: (body: CreateAdminAccount) => api.accounts.create(body),
    onSuccess: (data) => {
      setActiveAccountId(data.id);
      queryClient.invalidateQueries({ queryKey: ['accounts'] });
      toast({ kind: 'success', title: 'Konto dodane', description: 'Inbox zaraz się załaduje.' });
      onOpenChange(false);
    },
    onError: (err) =>
      toast({ kind: 'error', title: 'Nie udało się dodać', description: (err as Error).message }),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Nowe konto admina Circle</DialogTitle>
          <DialogDescription>
            Token Headless Auth jest skonfigurowany w .env serwera (BOOTSTRAP_ADMIN_TOKEN).
          </DialogDescription>
        </DialogHeader>
        <AccountForm
          submitLabel="Dodaj konto"
          pending={createMutation.isPending}
          onSubmit={(v) => createMutation.mutate(v)}
          onCancel={() => onOpenChange(false)}
        />
      </DialogContent>
    </Dialog>
  );
}

function EditAccountDialog({
  account,
  onClose,
}: {
  account: AdminAccount | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const { toast } = useToast();

  useRegisterAssistantContext(
    useMemo(
      () =>
        account
          ? {
              kind: 'account' as const,
              accountId: account.id,
              label: account.label,
              personaText: account.systemPrompt,
            }
          : { kind: 'none' as const },
      [account],
    ),
  );

  const updateMutation = useMutation({
    mutationFn: (body: CreateAdminAccount) => {
      if (!account) throw new Error('no account');
      return api.accounts.update(account.id, body);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['accounts'] });
      toast({ kind: 'success', title: 'Konto zaktualizowane' });
      onClose();
    },
    onError: (err) =>
      toast({ kind: 'error', title: 'Nie udało się zapisać', description: (err as Error).message }),
  });

  return (
    <Dialog open={account !== null} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Edytuj konto admina</DialogTitle>
          <DialogDescription>
            Zmiana persony wpływa na generowanie kolejnych draftów. Modele możesz zostawić puste —
            wtedy używamy globalnych defaultów z .env.
          </DialogDescription>
        </DialogHeader>
        {account && (
          <AccountForm
            initial={{
              label: account.label,
              email: account.email,
              systemPrompt: account.systemPrompt,
            }}
            submitLabel="Zapisz zmiany"
            pending={updateMutation.isPending}
            kbAccountId={account.id}
            onSubmit={(v) => updateMutation.mutate(v)}
            onCancel={onClose}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}
