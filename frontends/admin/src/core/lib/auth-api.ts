/**
 * Auth API client — same domain, cookie-based session.
 * `credentials: 'same-origin'` is default for fetch on same host,
 * but we set explicitly for clarity (cookie travels both ways).
 */

const BASE = '/api/auth';

export class AuthError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = 'AuthError';
  }
}

async function http<T>(method: 'GET' | 'POST', path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method,
    credentials: 'same-origin',
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed.error === 'string') message = parsed.error;
    } catch {
      if (text) message = text;
    }
    throw new AuthError(res.status, message);
  }
  return text ? (JSON.parse(text) as T) : (undefined as T);
}

export const authApi = {
  me: () =>
    http<{ authenticated: boolean; email?: string }>('GET', '/me'),
  login: (email: string, password: string) =>
    http<{ ok: true; email: string }>('POST', '/login', { email, password }),
  logout: () => http<{ ok: true }>('POST', '/logout'),
};
