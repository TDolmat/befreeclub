import { useEffect, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertTriangle,
  ArrowLeft,
  Loader2,
  RotateCcw,
  Search,
  Send,
  Sparkles,
  UserCircle2,
  Wand2,
} from 'lucide-react';
import { Button } from '@/core/components/ui/button';
import { Card, CardContent } from '@/core/components/ui/card';
import { Input } from '@/core/components/ui/input';
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
import { MemberCard } from '@/tools/circle-dm/components/MemberCard';
import { useToast } from '@/core/components/ui/toast';
import { useEnsureActiveAccount } from '@/tools/circle-dm/hooks/useAccounts';
import { useMembers } from '@/tools/circle-dm/hooks/useMembers';
import { api } from '@/tools/circle-dm/lib/api';

export function ComposePage() {
  const [params] = useSearchParams();
  const memberIdRaw = params.get('member');
  const memberId = memberIdRaw ? Number.parseInt(memberIdRaw, 10) : null;
  const adminAccountId = useEnsureActiveAccount();

  if (memberId === null) {
    return <PickRecipient adminAccountId={adminAccountId} />;
  }

  return <ComposeView memberId={memberId} adminAccountId={adminAccountId} />;
}

function PickRecipient({ adminAccountId }: { adminAccountId: number | null }) {
  const [search, setSearch] = useState('');
  const membersQuery = useMembers(adminAccountId, search);

  return (
    <div className="animate-fade-in max-w-3xl mx-auto">
      <div className="flex items-center gap-3 mb-5">
        <Link to="/circle-dm">
          <Button variant="ghost" size="sm">
            <ArrowLeft className="h-4 w-4" />
            Inbox
          </Button>
        </Link>
        <h1 className="auth-title text-2xl">Nowa wiadomość</h1>
      </div>

      <p className="text-sm text-foreground/60 mb-4">
        Wybierz odbiorcę z listy członków. Możesz pisać do kogokolwiek — jeśli to pierwsza
        rozmowa, Circle utworzy nowy wątek przy wysłaniu.
      </p>

      <div className="relative mb-4">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-foreground/40" />
        <Input
          autoFocus
          placeholder="Szukaj po imieniu, headline, emailu…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="pl-9"
        />
      </div>

      {membersQuery.isLoading && (
        <div className="flex items-center justify-center py-12 text-foreground/50">
          <Loader2 className="h-5 w-5 animate-spin mr-2" />
          Ładuję członków…
        </div>
      )}

      {membersQuery.data && membersQuery.data.length === 0 && (
        <Card>
          <CardContent className="py-8 text-center text-foreground/60">
            {search ? `Nic nie pasuje do "${search}"` : 'Brak członków w cache.'}
          </CardContent>
        </Card>
      )}

      {membersQuery.data && membersQuery.data.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
          {membersQuery.data.map((m, idx) => (
            <div
              key={m.id}
              className="animate-fade-in"
              style={{ animationDelay: `${Math.min(idx, 30) * 15}ms` }}
            >
              <MemberCard member={m} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ComposeView({
  memberId,
  adminAccountId,
}: {
  memberId: number;
  adminAccountId: number | null;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [text, setText] = useState('');
  const [busy, setBusy] = useState<null | 'generate' | 'format' | 'send'>(null);
  const [confirming, setConfirming] = useState(false);

  const memberQuery = useQuery({
    queryKey: ['member', memberId],
    queryFn: () => api.members.get(memberId),
  });

  const generateMutation = useMutation({
    mutationFn: () => {
      if (adminAccountId === null) throw new Error('No active account');
      if (!memberQuery.data) throw new Error('Member not loaded');
      setBusy('generate');
      return api.compose.generate(adminAccountId, memberQuery.data.circleCommunityMemberId);
    },
    onSuccess: (r) => {
      setText(r.draft);
      toast({ kind: 'info', title: 'Wygenerowano' });
    },
    onError: (err) =>
      toast({ kind: 'error', title: 'Błąd generowania', description: (err as Error).message }),
    onSettled: () => setBusy(null),
  });

  const formatMutation = useMutation({
    mutationFn: () => {
      if (adminAccountId === null) throw new Error('No active account');
      if (!memberQuery.data) throw new Error('Member not loaded');
      setBusy('format');
      return api.format.compose(adminAccountId, memberQuery.data.circleCommunityMemberId, text);
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
      if (adminAccountId === null) throw new Error('No active account');
      if (!memberQuery.data) throw new Error('Member not loaded');
      setBusy('send');
      return api.compose.send(adminAccountId, memberQuery.data.circleCommunityMemberId, text);
    },
    onSuccess: (r) => {
      setConfirming(false);
      if (r.ok) {
        toast({ kind: 'success', title: 'Wysłano', description: `Nowy wątek utworzony` });
        queryClient.invalidateQueries({ queryKey: ['threads'] });
        navigate(`/circle-dm/thread/${r.threadId}`);
      } else {
        toast({ kind: 'error', title: 'Circle odrzuciło', description: r.error });
      }
    },
    onError: (err) => {
      setConfirming(false);
      toast({ kind: 'error', title: 'Send error', description: (err as Error).message });
    },
    onSettled: () => setBusy(null),
  });

  if (memberQuery.isLoading) {
    return (
      <div className="flex items-center justify-center py-16 text-foreground/50">
        <Loader2 className="h-5 w-5 animate-spin mr-2" />
        Ładuję profil…
      </div>
    );
  }

  if (!memberQuery.data) {
    return (
      <div className="max-w-md mx-auto py-12">
        <Card>
          <CardContent className="py-8 text-center">
            <AlertTriangle className="h-8 w-8 text-destructive mx-auto mb-3" />
            <p className="text-foreground/80 mb-4">Nie znaleziono członka.</p>
            <Link to="/circle-dm/compose">
              <Button variant="outline" size="sm">
                Wybierz innego
              </Button>
            </Link>
          </CardContent>
        </Card>
      </div>
    );
  }

  const member = memberQuery.data;
  const canSend = text.trim().length > 0 && busy === null;

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
          <UserCircle2 className="h-6 w-6 text-primary" />
          Nowa wiadomość
        </h1>
      </div>

      <div className="grid lg:grid-cols-[1fr_minmax(360px,460px)] gap-4">
        {/* Lewa kolumna: profil osoby */}
        <Card>
          <CardContent className="p-5 flex flex-col gap-4">
            <div className="flex items-center gap-4">
              <Avatar name={member.name} url={member.avatarUrl} size="lg" />
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <h2 className="font-bold text-lg truncate">{member.name}</h2>
                  {member.isAdmin && (
                    <span className="badge-brand text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded">
                      admin
                    </span>
                  )}
                </div>
                {member.headline && (
                  <p className="text-sm text-foreground/70 mt-1">{member.headline}</p>
                )}
                {member.lastSeenText && (
                  <p className="text-xs text-foreground/40 mt-1">{member.lastSeenText}</p>
                )}
              </div>
            </div>

            {member.bio && (
              <div>
                <div className="text-[10px] uppercase tracking-wider text-foreground/40 mb-1">
                  Bio
                </div>
                <p className="text-sm text-foreground/75 whitespace-pre-wrap">{member.bio}</p>
              </div>
            )}

            {member.location && (
              <div>
                <div className="text-[10px] uppercase tracking-wider text-foreground/40 mb-1">
                  Lokalizacja
                </div>
                <p className="text-sm text-foreground/75">{member.location}</p>
              </div>
            )}

            {member.email && (
              <div>
                <div className="text-[10px] uppercase tracking-wider text-foreground/40 mb-1">
                  Email
                </div>
                <p className="text-sm text-foreground/75 break-all">{member.email}</p>
              </div>
            )}

          </CardContent>
        </Card>

        {/* Prawa kolumna: jedno pole + Formatuj z AI + Wyślij */}
        <Card glow className="h-fit lg:sticky lg:top-20">
          <CardContent className="p-4 flex flex-col gap-3">
            <h3 className="auth-title text-base flex items-center gap-2">
              <Sparkles className="h-4 w-4 text-primary" />
              Wiadomość
            </h3>

            <Textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder={
                busy === 'generate'
                  ? 'Generuję draft…'
                  : 'Wpisz treść albo brain dump z dyktowania.\n\nKliknij „Formatuj z AI" żeby przerobić to w finalną wiadomość zgodną z personą.'
              }
              rows={12}
              disabled={busy === 'generate' || busy === 'format'}
              className="font-sans text-sm leading-relaxed"
            />

            <div className="grid grid-cols-2 gap-2">
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
                variant="ghost"
                size="sm"
                onClick={() => generateMutation.mutate()}
                disabled={busy !== null}
                title="Wygeneruj zupełnie nową wiadomość biorąc pod uwagę profil tej osoby"
              >
                {busy === 'generate' ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <RotateCcw className="h-4 w-4" />
                )}
                Wygeneruj cold opener
              </Button>
            </div>

            <div className="pt-2 border-t border-border">
              <Button
                variant="default"
                className="w-full"
                disabled={!canSend}
                onClick={() => setConfirming(true)}
              >
                <Send className="h-4 w-4" />
                Wyślij wiadomość
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>

      <Dialog open={confirming} onOpenChange={setConfirming}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Wysłać pierwszą wiadomość do {member.name}?</DialogTitle>
            <DialogDescription>
              To utworzy nowy wątek DM w Circle. Nie da się tego cofnąć.
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
              Wyślij
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
