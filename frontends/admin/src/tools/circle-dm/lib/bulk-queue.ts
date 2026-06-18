const KEY = 'circle-dm:bulk-queue';

export type BulkQueueItem =
  | {
      kind: 'thread';
      threadId: number;
      name: string;
      avatarUrl: string | null;
      lastMessagePreview: string | null;
      lastMessageAt: string | null;
    }
  | {
      kind: 'member';
      memberId: number;
      name: string;
      avatarUrl: string | null;
      lastMessagePreview: string | null;
      lastMessageAt: string | null;
    };

export interface BulkQueue {
  adminAccountId: number;
  items: BulkQueueItem[];
}

export function setBulkQueue(q: BulkQueue): void {
  sessionStorage.setItem(KEY, JSON.stringify(q));
}

export function getBulkQueue(): BulkQueue | null {
  const raw = sessionStorage.getItem(KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as BulkQueue;
  } catch {
    return null;
  }
}

export function clearBulkQueue(): void {
  sessionStorage.removeItem(KEY);
}
