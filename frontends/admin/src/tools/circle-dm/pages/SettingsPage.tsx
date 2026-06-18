import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Loader2, LogOut, Save, Settings as SettingsIcon } from 'lucide-react';
import { Button } from '@/core/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/core/components/ui/card';
import { Input } from '@/core/components/ui/input';
import { Label } from '@/core/components/ui/label';
import { Textarea } from '@/core/components/ui/textarea';
import { useToast } from '@/core/components/ui/toast';
import { useAuth, useLogout } from '@/core/hooks/useAuth';
import { api } from '@/tools/circle-dm/lib/api';
import { KnowledgeAttach } from '@/tools/circle-dm/components/KnowledgeAttach';
import { useRegisterAssistantContext } from '@/tools/circle-dm/assistant/AssistantContext';

const META_PLACEHOLDER = `Globalne zasady stylu — dotyczą KAŻDEJ wiadomości i KAŻDEJ persony.

Przykład:
- Używaj krótkich myślników (-) zamiast długich (—).
- Bez emoji.
- Bez wykrzykników.
- "Cześć" zamiast "Witaj".`;

const FORMAT_PLACEHOLDER = `Instrukcja dla AI gdy klikam "Formatuj z AI" w wątku.

Przykład:
Przekształć podany tekst w finalną wiadomość DM w stylu persony admina, zachowując sens, ale dopasowując ton.

- Jeśli to brain dump — zrekonstruuj w pierwszej osobie.
- Jeśli to gotowy draft — popraw co trzeba, zachowaj sens.
- Krótko (1–4 zdania).
- Bez prefiksu "Oto:", bez wyjaśnień, bez cudzysłowów.`;

export function SettingsPage() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const auth = useAuth();
  const logout = useLogout();
  const settingsQuery = useQuery({
    queryKey: ['settings'],
    queryFn: () => api.settings.get(),
  });

  const [metaText, setMetaText] = useState('');
  const [formatText, setFormatText] = useState('');
  const [draftModel, setDraftModel] = useState('');
  const [formatModel, setFormatModel] = useState('');
  const [metaDirty, setMetaDirty] = useState(false);
  const [formatDirty, setFormatDirty] = useState(false);
  const [modelsDirty, setModelsDirty] = useState(false);

  useEffect(() => {
    if (settingsQuery.data) {
      setMetaText(settingsQuery.data.globalMetaPrompt);
      setFormatText(settingsQuery.data.formatPrompt);
      setDraftModel(settingsQuery.data.draftModel ?? '');
      setFormatModel(settingsQuery.data.formatModel ?? '');
      setMetaDirty(false);
      setFormatDirty(false);
      setModelsDirty(false);
    }
  }, [settingsQuery.data]);

  const saveMutation = useMutation({
    mutationFn: (patch: {
      globalMetaPrompt?: string;
      formatPrompt?: string;
      draftModel?: string | null;
      formatModel?: string | null;
    }) => api.settings.update(patch),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['settings'] });
      setMetaDirty(false);
      setFormatDirty(false);
      setModelsDirty(false);
      toast({ kind: 'success', title: 'Ustawienia zapisane' });
    },
    onError: (err) =>
      toast({ kind: 'error', title: 'Błąd zapisu', description: (err as Error).message }),
  });

  useRegisterAssistantContext(
    useMemo(
      () => ({ kind: 'settings' as const, metaPrompt: metaText, formatPrompt: formatText }),
      [metaText, formatText],
    ),
  );

  const onSaveMeta = () => saveMutation.mutate({ globalMetaPrompt: metaText });
  const onSaveFormat = () => saveMutation.mutate({ formatPrompt: formatText });
  const onSaveModels = () =>
    saveMutation.mutate({
      draftModel: draftModel.trim() || null,
      formatModel: formatModel.trim() || null,
    });

  return (
    <div className="max-w-3xl mx-auto animate-fade-in flex flex-col gap-4">
      <div className="flex items-center gap-3 mb-2">
        <div className="h-10 w-10 rounded-lg bg-primary/15 border border-primary/30 grid place-items-center">
          <SettingsIcon className="h-5 w-5 text-primary" />
        </div>
        <div>
          <h1 className="font-bold text-xl">Ustawienia globalne</h1>
          <p className="text-sm text-foreground/50">
            Dwa prompty wstrzykiwane do KAŻDEJ persony przy generowaniu i formatowaniu wiadomości.
          </p>
        </div>
      </div>

      <Card glow>
        <CardHeader>
          <CardTitle className="text-base">Globalne zasady stylu (meta-prompt)</CardTitle>
          <CardDescription>
            Pojawia się PRZED system promptem każdego konta zarówno przy generowaniu draftu, jak i
            przy "Formatuj z AI". Trzymaj się kategorii: interpunkcja, emoji, ton, terminologia.
            Krótko — to ma być wytrych, nie esej.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {settingsQuery.isLoading ? (
            <div className="flex items-center gap-2 text-foreground/40 py-8 justify-center">
              <Loader2 className="h-4 w-4 animate-spin" /> Ładuję…
            </div>
          ) : (
            <>
              <Textarea
                value={metaText}
                onChange={(e) => {
                  setMetaText(e.target.value);
                  setMetaDirty(true);
                }}
                placeholder={META_PLACEHOLDER}
                rows={10}
                className="font-mono text-xs leading-relaxed"
              />
              <div className="flex justify-end">
                <Button
                  variant="default"
                  size="sm"
                  onClick={onSaveMeta}
                  disabled={!metaDirty || saveMutation.isPending}
                >
                  {saveMutation.isPending ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <Save className="h-4 w-4" />
                  )}
                  Zapisz
                </Button>
              </div>
              <div className="border-t border-border/50 pt-3">
                <p className="text-[11px] text-foreground/40 mb-1.5">
                  Baza wiedzy globalna — pliki/tekst doklejane do KAŻDEJ generowanej i
                  formatowanej wiadomości (każde konto).
                </p>
                <KnowledgeAttach scope="global" />
              </div>
            </>
          )}
        </CardContent>
      </Card>

      <Card glow>
        <CardHeader>
          <CardTitle className="text-base">Prompt „Formatuj z AI”</CardTitle>
          <CardDescription>
            Instrukcja dla AI kiedy klikasz "Formatuj z AI" — co ma zrobić z tekstem który wpisałeś
            (brain dump, draft, krótka instrukcja). Doklejana do system promptu RAZEM z meta-promptem
            i personą konta. Bez wpływu na auto-generowanie draftu (to bierze tylko meta + persona +
            kontekst).
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {settingsQuery.isLoading ? (
            <div className="flex items-center gap-2 text-foreground/40 py-8 justify-center">
              <Loader2 className="h-4 w-4 animate-spin" /> Ładuję…
            </div>
          ) : (
            <>
              <Textarea
                value={formatText}
                onChange={(e) => {
                  setFormatText(e.target.value);
                  setFormatDirty(true);
                }}
                placeholder={FORMAT_PLACEHOLDER}
                rows={14}
                className="font-mono text-xs leading-relaxed"
              />
              <div className="flex items-center justify-between gap-3">
                <p className="text-[11px] text-foreground/40">
                  Puste pole = użyjemy wbudowanego defaultu.
                </p>
                <Button
                  variant="default"
                  size="sm"
                  onClick={onSaveFormat}
                  disabled={!formatDirty || saveMutation.isPending}
                >
                  {saveMutation.isPending ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <Save className="h-4 w-4" />
                  )}
                  Zapisz
                </Button>
              </div>
              <div className="border-t border-border/50 pt-3">
                <p className="text-[11px] text-foreground/40 mb-1.5">
                  Baza wiedzy globalna (ta sama pula co przy meta-promptcie).
                </p>
                <KnowledgeAttach scope="global" />
              </div>
            </>
          )}
        </CardContent>
      </Card>

      <Card glow>
        <CardHeader>
          <CardTitle className="text-base">Modele AI</CardTitle>
          <CardDescription>
            Globalne nazwy modeli Claude używanych do dwóch faz. Puste pole = domyślny model z
            <code className="text-foreground/70 mx-1 px-1 rounded bg-card-hover/60">.env</code>
            serwera (DRAFT_MODEL i POLISH_MODEL). Zmiana modelu nie wymaga restartu — cache 30s.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {settingsQuery.isLoading ? (
            <div className="flex items-center gap-2 text-foreground/40 py-8 justify-center">
              <Loader2 className="h-4 w-4 animate-spin" /> Ładuję…
            </div>
          ) : (
            <>
              <div className="grid sm:grid-cols-2 gap-3">
                <div>
                  <Label htmlFor="draftModel">Draft model (auto-generate + "Wygeneruj")</Label>
                  <Input
                    id="draftModel"
                    placeholder="claude-sonnet-4-6 (domyślny)"
                    value={draftModel}
                    onChange={(e) => {
                      setDraftModel(e.target.value);
                      setModelsDirty(true);
                    }}
                  />
                </div>
                <div>
                  <Label htmlFor="formatModel">Format model ("Formatuj z AI")</Label>
                  <Input
                    id="formatModel"
                    placeholder="claude-opus-4-7 (domyślny)"
                    value={formatModel}
                    onChange={(e) => {
                      setFormatModel(e.target.value);
                      setModelsDirty(true);
                    }}
                  />
                </div>
              </div>
              <div className="flex justify-end">
                <Button
                  variant="default"
                  size="sm"
                  onClick={onSaveModels}
                  disabled={!modelsDirty || saveMutation.isPending}
                >
                  {saveMutation.isPending ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <Save className="h-4 w-4" />
                  )}
                  Zapisz
                </Button>
              </div>
            </>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Sesja</CardTitle>
          <CardDescription>
            {auth.data?.email ? `Zalogowany jako ${auth.data.email}.` : 'Sesja aktywna.'} W
            dev (localhost) wylogowanie nie ma efektu - middleware bypassuje.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button
            variant="outline"
            size="sm"
            onClick={() => logout.mutate()}
            disabled={logout.isPending}
          >
            {logout.isPending ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <LogOut className="h-4 w-4" />
            )}
            Wyloguj
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}
