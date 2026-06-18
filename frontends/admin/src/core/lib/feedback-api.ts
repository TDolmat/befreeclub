import type { FeedbackItem, FeedbackStatus } from '@bfc/shared';

const BASE = '/api/feedback';

async function http<T>(
  method: 'GET' | 'POST' | 'PATCH' | 'DELETE',
  path: string,
  body?: unknown,
): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method,
    credentials: 'same-origin',
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  if (!res.ok) {
    if (res.status === 401) window.location.reload();
    let message = `${res.status} ${res.statusText}`;
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed.error === 'string') message = parsed.error;
    } catch {
      if (text) message = text;
    }
    throw new Error(message);
  }
  return text ? (JSON.parse(text) as T) : (undefined as T);
}

export const feedbackApi = {
  list: () => http<{ items: FeedbackItem[] }>('GET', ''),
  count: () => http<{ openCount: number }>('GET', '/count'),
  create: (body: string, scope: string) =>
    http<{ item: FeedbackItem }>('POST', '', { body, scope }),
  setStatus: (id: number, status: FeedbackStatus) =>
    http<{ ok: true }>('PATCH', `/${id}/status`, { status }),
  remove: (id: number) => http<{ ok: true }>('DELETE', `/${id}`),
};
