import { Sparkles } from 'lucide-react';
import { Button } from '@/core/components/ui/button';
import { cn } from '@/core/lib/utils';
import { useAssistant } from './AssistantContext';

export function AssistantToggle() {
  const { isOpen, toggle } = useAssistant();
  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={toggle}
      title="BFC AI (Cmd/Ctrl+J)"
      className={cn('text-primary', isOpen && 'bg-primary/15')}
    >
      <Sparkles className="h-4 w-4 fill-primary" />
      <span className="font-semibold">BFC AI</span>
    </Button>
  );
}
