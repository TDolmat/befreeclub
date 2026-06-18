const tz = 'Europe/Warsaw';

const dateTimeFull = new Intl.DateTimeFormat('pl-PL', {
  timeZone: tz,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
});

const timeOnly = new Intl.DateTimeFormat('pl-PL', {
  timeZone: tz,
  hour: '2-digit',
  minute: '2-digit',
});

const dateOnly = new Intl.DateTimeFormat('pl-PL', {
  timeZone: tz,
  day: '2-digit',
  month: '2-digit',
});

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  return dateTimeFull.format(new Date(iso));
}

/**
 * Human-friendly relative time: "5 min temu", "wczoraj 14:32", "7 dni temu", "22.04".
 */
export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return '';
  const d = new Date(iso);
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffMin = Math.floor(diffMs / 60_000);
  if (diffMin < 1) return 'przed chwilą';
  if (diffMin < 60) return `${diffMin} min temu`;
  const diffH = Math.floor(diffMin / 60);
  if (diffH < 24) return `${diffH} h temu`;
  const diffDays = Math.floor(diffH / 24);
  if (diffDays === 1) return `wczoraj ${timeOnly.format(d)}`;
  if (diffDays < 7) return `${diffDays} d temu`;
  return dateOnly.format(d);
}

export function getInitials(name: string | null | undefined): string {
  if (!name) return '?';
  const parts = name.trim().split(/\s+/);
  if (parts.length === 0) return '?';
  if (parts.length === 1) return parts[0]!.slice(0, 2).toUpperCase();
  return (parts[0]![0]! + parts[parts.length - 1]![0]!).toUpperCase();
}
