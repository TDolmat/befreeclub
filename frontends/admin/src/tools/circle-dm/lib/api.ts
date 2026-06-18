import type {
  AdminAccount,
  CommunityMember,
  ComposeDraftResult,
  ComposeSendResult,
  CreateAdminAccount,
  DmMessage,
  DmThread,
  DraftIteration,
  DraftSession,
  AssistantContext,
  AssistantConversation,
  AssistantConversationFull,
  AssistantMessage,
  KbDocument,
  KbDocumentDetail,
  KbListResponse,
  KbScope,
  TestConnectionResult,
  ThreadCheckup,
  ThreadFilter,
  ThreadSort,
  ThreadStatus,
  UpdateAdminAccount,
} from '@bfc/shared';

const BASE = '/api/circle-dm';

async function http<T>(
  method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE',
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
    if (res.status === 401) {
      // Session expired or revoked — force whole-app re-render via the auth
      // gate (App.tsx checks useAuth before rendering anything). Reloading
      // pulls /api/auth/me fresh, which will return authenticated:false,
      // which renders LoginPage. Simpler than a global event bus.
      window.location.reload();
    }
    let message = `${res.status} ${res.statusText}`;
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed.error === 'string') message = parsed.error;
    } catch {
      if (text) message = text;
    }
    throw new ApiError(res.status, message);
  }
  return text ? (JSON.parse(text) as T) : (undefined as T);
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

// ─── Accounts ─────────────────────────────────────────────────────────────────

export const api = {
  accounts: {
    list: () => http<{ accounts: AdminAccount[] }>('GET', '/accounts'),
    create: (body: CreateAdminAccount) => http<{ id: number }>('POST', '/accounts', body),
    update: (id: number, body: UpdateAdminAccount) =>
      http<{ ok: true }>('PATCH', `/accounts/${id}`, body),
    remove: (id: number) => http<{ ok: true }>('DELETE', `/accounts/${id}`),
    testConnection: (id: number) =>
      http<TestConnectionResult>('POST', `/accounts/${id}/test-connection`),
    sync: (id: number) =>
      http<{ ok: boolean; changedThreadIds: number[]; newUnreadThreadIds: number[] }>(
        'POST',
        `/accounts/${id}/sync`,
      ),
  },
  threads: {
    list: (params: {
      adminAccountId: number;
      filter?: ThreadFilter;
      sort?: ThreadSort;
      limit?: number;
    }) => {
      const sp = new URLSearchParams();
      sp.set('adminAccountId', String(params.adminAccountId));
      if (params.filter) sp.set('filter', params.filter);
      if (params.sort) sp.set('sort', params.sort);
      if (params.limit) sp.set('limit', String(params.limit));
      return http<{ threads: DmThread[]; count: number }>('GET', `/threads?${sp.toString()}`);
    },
    get: (id: number) => http<DmThread>('GET', `/threads/${id}`),
    messages: (id: number, refetch = false) =>
      http<{ messages: DmMessage[] }>(
        'GET',
        `/threads/${id}/messages${refetch ? '?refetch=1' : ''}`,
      ),
    setStatus: (id: number, status: ThreadStatus) =>
      http<{ ok: true }>('PATCH', `/threads/${id}/status`, { status }),
    setFlagged: (id: number, isFlagged: boolean) =>
      http<{ ok: true }>('PATCH', `/threads/${id}/flag`, { isFlagged }),
    bulkAction: (
      adminAccountId: number,
      ids: number[],
      action: 'done' | 'inbox' | 'flag' | 'unflag',
    ) =>
      http<{ ok: true; count: number }>('POST', '/threads/bulk-action', {
        adminAccountId,
        ids,
        action,
      }),
    listCheckups: (id: number) =>
      http<{ checkups: ThreadCheckup[] }>('GET', `/threads/${id}/checkups`),
    createCheckup: (id: number, body: { dueAt: string; note?: string | null }) =>
      http<ThreadCheckup>('POST', `/threads/${id}/checkups`, body),
    markCheckupDone: (id: number, checkupId: number) =>
      http<{ ok: true }>('PATCH', `/threads/${id}/checkups/${checkupId}/done`),
    deleteCheckup: (id: number, checkupId: number) =>
      http<{ ok: true }>('DELETE', `/threads/${id}/checkups/${checkupId}`),
  },
  messages: {
    retryTranscript: (messageId: number) =>
      http<{ ok: true }>('POST', `/messages/${messageId}/transcribe-retry`),
    retryImageDescription: (messageId: number, descId: number) =>
      http<{ ok: true }>(
        'POST',
        `/messages/${messageId}/image-descriptions/${descId}/retry`,
      ),
  },
  drafts: {
    get: (threadId: number) =>
      http<{ session: DraftSession | null; iterations: DraftIteration[] }>(
        'GET',
        `/drafts/${threadId}`,
      ),
    generate: (threadId: number) => http<{ ok: true }>('POST', `/drafts/${threadId}/generate`),
    update: (threadId: number, draft: string) =>
      http<{ ok: true }>('PATCH', `/drafts/${threadId}`, { draft }),
    reset: (threadId: number) => http<{ ok: true }>('DELETE', `/drafts/${threadId}`),
    send: (threadId: number, body: string) =>
      http<{ ok: boolean; circleMessageId: number | null; error?: string }>(
        'POST',
        `/drafts/${threadId}/send`,
        { body },
      ),
  },
  members: {
    list: (params: {
      adminAccountId: number;
      q?: string;
      limit?: number;
      excludeWithThread?: boolean;
    }) => {
      const sp = new URLSearchParams();
      sp.set('adminAccountId', String(params.adminAccountId));
      if (params.q) sp.set('q', params.q);
      if (params.limit) sp.set('limit', String(params.limit));
      if (params.excludeWithThread) sp.set('excludeWithThread', '1');
      return http<{ members: CommunityMember[]; count: number }>('GET', `/members?${sp.toString()}`);
    },
    get: (id: number) => http<CommunityMember>('GET', `/members/${id}`),
    sync: (adminAccountId: number) =>
      http<{ ok: true; syncedCount: number }>('POST', '/members/sync', { adminAccountId }),
  },
  compose: {
    generate: (adminAccountId: number, circleCommunityMemberId: number) =>
      http<ComposeDraftResult>('POST', '/compose/generate', {
        adminAccountId,
        circleCommunityMemberId,
      }),
    send: (adminAccountId: number, circleCommunityMemberId: number, body: string) =>
      http<ComposeSendResult>('POST', '/compose/send', {
        adminAccountId,
        circleCommunityMemberId,
        body,
      }),
  },
  format: {
    thread: (threadId: number, text: string) =>
      http<{ text: string }>(
        'POST',
        '/format/thread',
        { threadId, text },
      ),
    compose: (adminAccountId: number, circleCommunityMemberId: number, text: string) =>
      http<{ text: string }>(
        'POST',
        '/format/compose',
        { adminAccountId, circleCommunityMemberId, text },
      ),
    bulk: (adminAccountId: number, text: string) =>
      http<{ text: string }>(
        'POST',
        '/format/bulk',
        { adminAccountId, text },
      ),
  },
  bulk: {
    send: (
      items: Array<
        | { kind: 'thread'; threadId: number }
        | { kind: 'member'; adminAccountId: number; memberId: number }
      >,
      body: string,
    ) =>
      http<{
        totalCount: number;
        okCount: number;
        results: Array<{
          kind: 'thread' | 'member';
          threadId: number | null;
          memberId: number | null;
          ok: boolean;
          circleMessageId: number | null;
          error?: string;
        }>;
      }>('POST', '/bulk/send', { items, body }),
  },
  settings: {
    get: () =>
      http<{
        globalMetaPrompt: string;
        formatPrompt: string;
        draftModel: string | null;
        formatModel: string | null;
        noReplyThresholdDays: number;
        silenceThresholdDays: number;
      }>('GET', '/settings'),
    update: (patch: {
      globalMetaPrompt?: string;
      formatPrompt?: string;
      draftModel?: string | null;
      formatModel?: string | null;
      noReplyThresholdDays?: number;
      silenceThresholdDays?: number;
    }) => http<{ ok: true }>('PUT', '/settings', patch),
  },
  kb: {
    list: (scope: KbScope, accountId?: number) => {
      const sp = new URLSearchParams({ scope });
      if (accountId) sp.set('accountId', String(accountId));
      return http<KbListResponse>('GET', `/kb?${sp.toString()}`);
    },
    get: (id: number) => http<KbDocumentDetail>('GET', `/kb/${id}`),
    createManual: (body: {
      scope: KbScope;
      adminAccountId?: number | null;
      title: string;
      bodyText: string;
    }) => http<{ id: number }>('POST', '/kb', body),
    upload: async (
      scope: KbScope,
      file: File,
      opts: { title?: string; adminAccountId?: number } = {},
    ) => {
      const fd = new FormData();
      fd.append('file', file);
      fd.append('scope', scope);
      if (opts.title) fd.append('title', opts.title);
      if (opts.adminAccountId) fd.append('adminAccountId', String(opts.adminAccountId));
      const res = await fetch(`${BASE}/kb/upload`, {
        method: 'POST',
        credentials: 'same-origin',
        body: fd,
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
        throw new ApiError(res.status, message);
      }
      return JSON.parse(text) as { id: number; tokenEstimate: number };
    },
    update: (
      id: number,
      patch: { title?: string; bodyText?: string; enabled?: boolean },
    ) => http<{ ok: true }>('PATCH', `/kb/${id}`, patch),
    remove: (id: number) => http<{ ok: true }>('DELETE', `/kb/${id}`),
    originalUrl: (id: number) => `${BASE}/kb/${id}/original`,
  },
  assistant: {
    listConversations: () =>
      http<{ conversations: AssistantConversation[] }>('GET', '/assistant/conversations'),
    getConversation: (id?: number) =>
      http<AssistantConversationFull>(
        'GET',
        id ? `/assistant/conversation?id=${id}` : '/assistant/conversation',
      ),
    newConversation: () => http<AssistantConversationFull>('POST', '/assistant/new'),
    deleteConversation: (id: number) =>
      http<{ ok: boolean }>('DELETE', `/assistant/conversation/${id}`),
    turn: (conversationId: number, message: string, context: AssistantContext) =>
      http<{
        ok: true;
        userMessageId: number;
        assistantMessageId: number;
        hasAction: boolean;
      }>('POST', '/assistant/turn', { conversationId, message, context }),
    applyMessage: (id: number) =>
      http<{ ok: boolean; message: AssistantMessage | null; error?: string }>(
        'POST',
        `/assistant/messages/${id}/apply`,
      ),
    dismissMessage: (id: number) =>
      http<{ ok: true }>('POST', `/assistant/messages/${id}/dismiss`),
    cancel: (conversationId: number) =>
      http<{ ok: boolean }>('POST', '/assistant/cancel', { conversationId }),
  },
};

export type { KbDocument };

export function listThreadsUrl(params: {
  adminAccountId: number;
  filter?: import('@bfc/shared').ThreadFilter;
  sort?: import('@bfc/shared').ThreadSort;
}) {
  return params;
}
