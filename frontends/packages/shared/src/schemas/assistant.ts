import { z } from 'zod';

// ─── Context snapshot — what the user is looking at ──────────────────────────
// Pages register one of these via useRegisterAssistantContext; the active
// snapshot is sent on every turn so the model knows where you are.

export const assistantContextSchema = z.discriminatedUnion('kind', [
  z.object({
    kind: z.literal('inbox'),
    adminAccountId: z.number().int().positive().nullable(),
    filter: z.string(),
    sort: z.string(),
    query: z.string(),
  }),
  z.object({
    kind: z.literal('thread'),
    adminAccountId: z.number().int().positive(),
    threadId: z.number().int().positive(),
    recipientName: z.string().nullable(),
    persona: z.string(),
    accountLabel: z.string(),
    draftText: z.string(),
    // Tail of the conversation, formatted like history-formatter output.
    historyExcerpt: z.string(),
  }),
  z.object({
    kind: z.literal('compose'),
    adminAccountId: z.number().int().positive(),
    memberId: z.number().int().positive(),
    memberName: z.string(),
    persona: z.string(),
    accountLabel: z.string(),
    currentText: z.string(),
    memberProfile: z.string(),
  }),
  z.object({
    kind: z.literal('settings'),
    metaPrompt: z.string(),
    formatPrompt: z.string(),
  }),
  z.object({
    kind: z.literal('account'),
    accountId: z.number().int().positive(),
    label: z.string(),
    personaText: z.string(),
  }),
  z.object({
    kind: z.literal('none'),
  }),
]);
export type AssistantContext = z.infer<typeof assistantContextSchema>;

// ─── Action proposal — model's "tool call" emitted as a JSON block ───────────

export const actionProposalSchema = z.discriminatedUnion('action', [
  z.object({
    action: z.literal('setDraft'),
    params: z.object({
      threadId: z.number().int().positive(),
      newText: z.string().min(1),
    }),
    preview: z.string(),
  }),
  z.object({
    action: z.literal('setPersona'),
    params: z.object({
      accountId: z.number().int().positive(),
      newText: z.string().min(10),
    }),
    preview: z.string(),
  }),
  z.object({
    action: z.literal('setGlobalMetaPrompt'),
    params: z.object({ newText: z.string() }),
    preview: z.string(),
  }),
  z.object({
    action: z.literal('setFormatPrompt'),
    params: z.object({ newText: z.string() }),
    preview: z.string(),
  }),
  z.object({
    action: z.literal('setKbDoc'),
    params: z.object({
      id: z.number().int().positive(),
      title: z.string().min(1).max(200).optional(),
      bodyText: z.string().max(500_000).optional(),
    }),
    preview: z.string(),
  }),
  z.object({
    action: z.literal('createKbManual'),
    params: z.object({
      scope: z.enum(['global', 'account']),
      adminAccountId: z.number().int().positive().nullable().optional(),
      title: z.string().min(1).max(200),
      bodyText: z.string().min(1).max(500_000),
    }),
    preview: z.string(),
  }),
]);
export type ActionProposal = z.infer<typeof actionProposalSchema>;
export type ActionKind = ActionProposal['action'];

// ─── DTOs ───────────────────────────────────────────────────────────────────

export const assistantMessageRoleSchema = z.enum(['user', 'assistant']);
export type AssistantMessageRole = z.infer<typeof assistantMessageRoleSchema>;

export const assistantMessageSchema = z.object({
  id: z.number().int().positive(),
  conversationId: z.number().int().positive(),
  role: assistantMessageRoleSchema,
  content: z.string(),
  actionProposal: actionProposalSchema.nullable(),
  appliedAt: z.string().datetime().nullable(),
  applyError: z.string().nullable(),
  createdAt: z.string().datetime(),
});
export type AssistantMessage = z.infer<typeof assistantMessageSchema>;

export const assistantConversationSchema = z.object({
  id: z.number().int().positive(),
  title: z.string().nullable(),
  lastMessageAt: z.string().datetime().nullable(),
  createdAt: z.string().datetime(),
});
export type AssistantConversation = z.infer<typeof assistantConversationSchema>;

export const assistantConversationFullSchema = z.object({
  conversation: assistantConversationSchema,
  messages: z.array(assistantMessageSchema),
});
export type AssistantConversationFull = z.infer<typeof assistantConversationFullSchema>;

// ─── Turn request ───────────────────────────────────────────────────────────

export const assistantTurnRequestSchema = z.object({
  message: z.string().min(1).max(4000),
  context: assistantContextSchema,
});
export type AssistantTurnRequest = z.infer<typeof assistantTurnRequestSchema>;
