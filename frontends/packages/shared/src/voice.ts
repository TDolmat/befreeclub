import type {
  ImageDescriptionStatus,
  VoiceTranscriptStatus,
} from './schemas/message.js';

export function formatVoiceDuration(sec: number | null): string {
  if (sec === null || sec < 0) return '?';
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}m${s.toString().padStart(2, '0')}s`;
}

/**
 * Render a voice-message line for AI context (history excerpt sent to Claude).
 * Keeps the spoken-message marker explicit so the model treats it as a
 * transcript (potentially with STT errors) rather than typed text.
 */
export function formatVoiceForAi(
  durationSec: number | null,
  status: VoiceTranscriptStatus | null,
  transcript: string | null,
): string {
  const dur = formatVoiceDuration(durationSec);
  if (status === 'done' && transcript) {
    return `[głosówka ${dur}, transkrypt]: "${transcript}"`;
  }
  if (status === 'pending') {
    return `[głosówka ${dur}, transkrypcja jeszcze nie gotowa]`;
  }
  if (status === 'error') {
    return `[głosówka ${dur}, transkrypcja nieudana]`;
  }
  return `[głosówka ${dur}]`;
}

/**
 * Render an image-attachment line for AI context. One per image in the
 * message. Mirrors voice formatting style so the model parses both the same
 * way.
 */
export function formatImageForAi(
  status: ImageDescriptionStatus | null,
  description: string | null,
): string {
  if (status === 'done' && description) {
    return `[zdjęcie]: "${description}"`;
  }
  if (status === 'pending') {
    return `[zdjęcie, opis jeszcze nie gotowy]`;
  }
  if (status === 'error') {
    return `[zdjęcie, opis nieudany]`;
  }
  return `[zdjęcie]`;
}
