import { useEffect, useState } from 'react';
import { ChevronDown, ChevronRight, Download, FileText, Loader2, Mic, RotateCcw, X } from 'lucide-react';
import {
  type DmAttachment,
  type DmImageDescription,
  type VoiceTranscriptStatus,
  formatVoiceDuration,
} from '@bfc/shared';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '@/tools/circle-dm/lib/api';
import { cn } from '@/core/lib/utils';

function humanSize(bytes: number | null): string {
  if (bytes === null) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

export interface VoiceTranscriptInfo {
  messageId: number;
  threadId: number;
  transcript: string | null;
  status: VoiceTranscriptStatus | null;
  error: string | null;
  durationSec: number | null;
}

export function MessageAttachments({
  attachments,
  isMine,
  voice,
  imageDescriptions,
  messageId,
  threadId,
}: {
  attachments: DmAttachment[];
  isMine: boolean;
  voice?: VoiceTranscriptInfo;
  imageDescriptions?: DmImageDescription[];
  messageId?: number;
  threadId?: number;
}) {
  if (attachments.length === 0) return null;
  const descByIdx = new Map<number, DmImageDescription>();
  for (const d of imageDescriptions ?? []) descByIdx.set(d.attachmentIndex, d);
  return (
    <div className="mt-2 flex flex-col gap-2">
      {attachments.map((a, i) => (
        <AttachmentItem
          key={i}
          att={a}
          isMine={isMine}
          voice={a.kind === 'audio' && a.voiceMessage ? voice : undefined}
          imageDescription={a.kind === 'image' ? descByIdx.get(i) : undefined}
          messageId={messageId}
          threadId={threadId}
        />
      ))}
    </div>
  );
}

function AttachmentItem({
  att,
  isMine,
  voice,
  imageDescription,
  messageId,
  threadId,
}: {
  att: DmAttachment;
  isMine: boolean;
  voice?: VoiceTranscriptInfo;
  imageDescription?: DmImageDescription;
  messageId?: number;
  threadId?: number;
}) {
  const [lightbox, setLightbox] = useState(false);

  if (att.kind === 'image') {
    const src = att.thumbnailUrl ?? att.url;
    const full = att.fullUrl ?? att.url;
    return (
      <div className="flex flex-col gap-1 max-w-xs">
        <button
          type="button"
          onClick={() => setLightbox(true)}
          title={`${att.filename} (${humanSize(att.byteSize)}) - klik aby powiększyć`}
          className="block"
        >
          <img
            src={src}
            alt={att.filename}
            loading="lazy"
            className="rounded-md max-h-72 w-auto h-auto object-contain border border-border/40 bg-card-hover/30 hover:opacity-90 transition-opacity cursor-zoom-in"
          />
        </button>
        {imageDescription && messageId !== undefined && threadId !== undefined && (
          <ImageDescriptionDisclosure
            desc={imageDescription}
            messageId={messageId}
            threadId={threadId}
          />
        )}
        {lightbox && (
          <ImageLightbox
            src={full}
            filename={att.filename}
            byteSize={att.byteSize}
            onClose={() => setLightbox(false)}
          />
        )}
      </div>
    );
  }

  if (att.kind === 'video') {
    return (
      <video
        src={att.url}
        controls
        preload="metadata"
        className="rounded-md max-h-72 max-w-xs border border-border/40 bg-card-hover/30"
      >
        <track kind="captions" />
      </video>
    );
  }

  if (att.kind === 'audio') {
    return (
      <div
        className={cn(
          'rounded-md border flex flex-col',
          isMine ? 'border-primary/20 bg-primary/5' : 'border-border bg-card-hover/40',
        )}
      >
        <div className="px-3 py-2 flex items-center gap-2">
          {att.voiceMessage && <Mic className="h-4 w-4 text-foreground/50 shrink-0" />}
          <audio src={att.url} controls preload="metadata" className="max-w-full">
            <track kind="captions" />
          </audio>
        </div>
        {voice && <VoiceTranscriptDisclosure voice={voice} />}
      </div>
    );
  }

  // Generic file (pdf, docx, ...)
  return (
    <a
      href={att.url}
      target="_blank"
      rel="noreferrer"
      download={att.filename}
      className={cn(
        'inline-flex items-center gap-2 rounded-md border px-3 py-2 max-w-xs hover:border-primary/40 transition-colors',
        isMine ? 'border-primary/20 bg-primary/5' : 'border-border bg-card-hover/40',
      )}
      title={att.contentType}
    >
      <FileText className="h-5 w-5 shrink-0 text-foreground/60" />
      <div className="flex-1 min-w-0">
        <div className="text-xs font-medium truncate">{att.filename}</div>
        <div className="text-[10px] text-foreground/40">
          {humanSize(att.byteSize)}
          {att.byteSize !== null && ' · '}
          {att.contentType}
        </div>
      </div>
      <Download className="h-4 w-4 shrink-0 text-foreground/40" />
    </a>
  );
}

function ImageLightbox({
  src,
  filename,
  byteSize,
  onClose,
}: {
  src: string;
  filename: string;
  byteSize: number | null;
  onClose: () => void;
}) {
  // ESC zamyka.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    // Block body scroll while lightbox is open.
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      window.removeEventListener('keydown', onKey);
      document.body.style.overflow = prev;
    };
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 bg-black/85 backdrop-blur-sm flex items-center justify-center p-6"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      {/* Top bar: filename + actions */}
      <div
        className="absolute top-4 left-4 right-4 flex items-center gap-3 z-10"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="text-white/80 text-sm truncate flex-1">
          {filename}
          {byteSize !== null && (
            <span className="text-white/40 ml-2">{humanSize(byteSize)}</span>
          )}
        </div>
        <a
          href={src}
          download={filename}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1.5 rounded-md bg-white/10 hover:bg-white/20 text-white px-3 py-1.5 text-sm transition-colors"
          title="Pobierz"
        >
          <Download className="h-4 w-4" />
          Pobierz
        </a>
        <button
          type="button"
          onClick={onClose}
          className="rounded-md bg-white/10 hover:bg-white/20 text-white p-1.5 transition-colors"
          title="Zamknij (Esc)"
        >
          <X className="h-5 w-5" />
        </button>
      </div>

      {/* Image */}
      <img
        src={src}
        alt={filename}
        onClick={(e) => e.stopPropagation()}
        className="max-h-[90vh] max-w-[95vw] object-contain shadow-2xl rounded-md cursor-default"
      />
    </div>
  );
}

function ImageDescriptionDisclosure({
  desc,
  messageId,
  threadId,
}: {
  desc: DmImageDescription;
  messageId: number;
  threadId: number;
}) {
  const [open, setOpen] = useState(false);
  const queryClient = useQueryClient();
  const retry = useMutation({
    mutationFn: () => api.messages.retryImageDescription(messageId, desc.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['thread', threadId, 'messages'] });
    },
  });

  if (desc.status === 'pending') {
    return (
      <div className="flex items-center gap-1.5 text-[11px] text-foreground/50 px-0.5">
        <Loader2 className="h-3 w-3 animate-spin" />
        <span>Generuję opis...</span>
      </div>
    );
  }
  if (desc.status === 'error') {
    return (
      <div className="flex items-center gap-2 text-[11px] px-0.5">
        <span className="text-destructive/80">Opis nieudany</span>
        <button
          type="button"
          onClick={() => retry.mutate()}
          disabled={retry.isPending}
          className="inline-flex items-center gap-1 text-foreground/60 hover:text-foreground transition-colors disabled:opacity-50"
        >
          <RotateCcw className={cn('h-3 w-3', retry.isPending && 'animate-spin')} />
          <span>Ponów</span>
        </button>
      </div>
    );
  }
  if (desc.status === 'done' && desc.description) {
    return (
      <div className="text-[11px] px-0.5">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="inline-flex items-center gap-1 text-foreground/60 hover:text-foreground transition-colors"
        >
          {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
          <span>{open ? 'Ukryj opis' : 'Pokaż opis'}</span>
        </button>
        {open && (
          <div className="mt-1 text-foreground/80 whitespace-pre-wrap leading-relaxed">
            {desc.description}
          </div>
        )}
      </div>
    );
  }
  return null;
}

function VoiceTranscriptDisclosure({ voice }: { voice: VoiceTranscriptInfo }) {
  const [open, setOpen] = useState(false);
  const queryClient = useQueryClient();
  const retry = useMutation({
    mutationFn: () => api.messages.retryTranscript(voice.messageId),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ['thread', voice.threadId, 'messages'],
      });
    },
  });

  const dur = formatVoiceDuration(voice.durationSec);
  const isPending = voice.status === 'pending';
  const isError = voice.status === 'error';
  const isDone = voice.status === 'done' && voice.transcript;

  let label: React.ReactNode;
  if (isPending) {
    label = (
      <>
        <Loader2 className="h-3.5 w-3.5 animate-spin text-foreground/50" />
        <span className="text-foreground/60">Transkrybuję... ({dur})</span>
      </>
    );
  } else if (isError) {
    label = (
      <>
        <span className="text-destructive/80">Transkrypcja nieudana ({dur})</span>
      </>
    );
  } else if (isDone) {
    label = (
      <>
        {open ? (
          <ChevronDown className="h-3.5 w-3.5 text-foreground/50" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 text-foreground/50" />
        )}
        <span className="text-foreground/70">
          {open ? 'Ukryj transkrypt' : 'Pokaż transkrypt'} ({dur})
        </span>
      </>
    );
  } else {
    return null;
  }

  return (
    <div className="border-t border-border/40 px-3 py-1.5 text-xs">
      <div className="flex items-center gap-2">
        {isDone ? (
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="inline-flex items-center gap-1.5 hover:text-foreground transition-colors"
          >
            {label}
          </button>
        ) : (
          <div className="inline-flex items-center gap-1.5">{label}</div>
        )}
        {isError && (
          <button
            type="button"
            onClick={() => retry.mutate()}
            disabled={retry.isPending}
            className="ml-auto inline-flex items-center gap-1 text-foreground/60 hover:text-foreground transition-colors disabled:opacity-50"
            title="Spróbuj transkrybować ponownie"
          >
            <RotateCcw className={cn('h-3 w-3', retry.isPending && 'animate-spin')} />
            <span>Spróbuj ponownie</span>
          </button>
        )}
      </div>
      {isDone && open && (
        <div className="mt-1.5 pb-1 text-foreground/85 whitespace-pre-wrap leading-relaxed">
          {voice.transcript}
        </div>
      )}
      {isError && voice.error && (
        <div className="mt-1 text-[10px] text-foreground/40 truncate" title={voice.error}>
          {voice.error}
        </div>
      )}
    </div>
  );
}
