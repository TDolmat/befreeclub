import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Check, Loader2, X } from 'lucide-react';
import type { ActionProposal, AssistantMessage } from '@bfc/shared';
import { Button } from '@/core/components/ui/button';
import { useToast } from '@/core/components/ui/toast';
import { api } from '@/tools/circle-dm/lib/api';
import { cn } from '@/core/lib/utils';

const ACTION_LABEL: Record<ActionProposal['action'], string> = {
  setDraft: 'Edycja drafta',
  setPersona: 'Edycja persony',
  setGlobalMetaPrompt: 'Edycja meta-promptu',
  setFormatPrompt: 'Edycja promptu "Formatuj z AI"',
  setKbDoc: 'Edycja dokumentu KB',
  createKbManual: 'Nowy dokument KB',
};

function describeNewText(proposal: ActionProposal): string {
  switch (proposal.action) {
    case 'setDraft':
      return proposal.params.newText;
    case 'setPersona':
      return proposal.params.newText;
    case 'setGlobalMetaPrompt':
    case 'setFormatPrompt':
      return proposal.params.newText;
    case 'setKbDoc':
      return proposal.params.bodyText ?? `(zmieniony tytuł na "${proposal.params.title ?? ''}")`;
    case 'createKbManual':
      return `${proposal.params.title}\n\n${proposal.params.bodyText}`;
  }
}

export function ActionProposalCard({ msg }: { msg: AssistantMessage }) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const proposal = msg.actionProposal;
  if (!proposal) return null;

  const applied = msg.appliedAt !== null;
  const dismissed = !applied && msg.applyError === 'dismissed';
  const failed = !applied && msg.applyError !== null && !dismissed;

  const applyMutation = useMutation({
    mutationFn: () => api.assistant.applyMessage(msg.id),
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ['assistant', 'conversation'] });
      // Domain-specific invalidations so the changed area refreshes.
      switch (proposal.action) {
        case 'setDraft':
          queryClient.invalidateQueries({
            queryKey: ['draft', proposal.params.threadId],
          });
          break;
        case 'setPersona':
          queryClient.invalidateQueries({ queryKey: ['accounts'] });
          break;
        case 'setGlobalMetaPrompt':
        case 'setFormatPrompt':
          queryClient.invalidateQueries({ queryKey: ['settings'] });
          break;
        case 'setKbDoc':
        case 'createKbManual':
          queryClient.invalidateQueries({ queryKey: ['kb'] });
          break;
      }
      if (res.ok) toast({ kind: 'success', title: 'Zastosowano' });
      else toast({ kind: 'error', title: 'Nie udało się', description: res.error });
    },
    onError: (err) =>
      toast({ kind: 'error', title: 'Apply failed', description: (err as Error).message }),
  });

  const dismissMutation = useMutation({
    mutationFn: () => api.assistant.dismissMessage(msg.id),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ['assistant', 'conversation'] }),
  });

  const preview = describeNewText(proposal);
  const truncated = preview.length > 600 ? `${preview.slice(0, 600)}\n... [skrócone]` : preview;

  return (
    <div
      className={cn(
        'rounded-lg border p-3 text-xs flex flex-col gap-2 bg-card-hover/40',
        applied ? 'border-success/40' : failed ? 'border-destructive/50' : 'border-primary/40',
      )}
    >
      <div className="flex items-center gap-2 font-semibold text-[11px] uppercase tracking-wider text-foreground/60">
        Propozycja: {ACTION_LABEL[proposal.action]}
        {applied && (
          <span className="ml-auto text-success font-medium flex items-center gap-1">
            <Check className="h-3 w-3" /> Zastosowano
          </span>
        )}
        {dismissed && <span className="ml-auto text-foreground/40">Odrzucone</span>}
        {failed && <span className="ml-auto text-destructive">Błąd</span>}
      </div>
      <p className="text-foreground/85 leading-relaxed">{proposal.preview}</p>
      <pre className="bg-background/40 border border-border/50 rounded p-2 whitespace-pre-wrap break-words max-h-48 overflow-y-auto font-mono text-[11px] leading-relaxed">
        {truncated}
      </pre>
      {failed && <p className="text-destructive text-[11px]">{msg.applyError}</p>}
      {!applied && !dismissed && (
        <div className="flex justify-end gap-2 mt-1">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => dismissMutation.mutate()}
            disabled={dismissMutation.isPending || applyMutation.isPending}
          >
            <X className="h-3.5 w-3.5" />
            Odrzuć
          </Button>
          <Button
            variant="default"
            size="sm"
            onClick={() => applyMutation.mutate()}
            disabled={applyMutation.isPending}
          >
            {applyMutation.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Check className="h-3.5 w-3.5" />
            )}
            Zastosuj{failed ? ' ponownie' : ''}
          </Button>
        </div>
      )}
    </div>
  );
}
