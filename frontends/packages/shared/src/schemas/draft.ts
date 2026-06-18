import { z } from 'zod';

export const draftStatusSchema = z.enum([
  'idle',
  'generating',
  'has_draft',
  'polishing',
  'ready_to_send',
  'sent',
  'error',
]);
export type DraftStatus = z.infer<typeof draftStatusSchema>;

export const iterationKindSchema = z.enum(['initial', 'user_feedback', 'polish']);
export type IterationKind = z.infer<typeof iterationKindSchema>;

export const draftIterationSchema = z.object({
  id: z.number().int().positive(),
  draftSessionId: z.number().int().positive(),
  iterationKind: iterationKindSchema,
  userInstruction: z.string().nullable(),
  draftText: z.string(),
  tokensUsed: z.number().int().nullable(),
  costUsd: z.number().nullable(),
  createdAt: z.string().datetime(),
});

export type DraftIteration = z.infer<typeof draftIterationSchema>;

export const draftSessionSchema = z.object({
  id: z.number().int().positive(),
  threadId: z.number().int().positive(),
  claudeSessionId: z.string().uuid(),
  status: draftStatusSchema,
  currentDraft: z.string().nullable(),
  iterationsCount: z.number().int().min(0),
  lastError: z.string().nullable(),
  createdAt: z.string().datetime(),
  updatedAt: z.string().datetime(),
});

export type DraftSession = z.infer<typeof draftSessionSchema>;

export const generateDraftRequestSchema = z.object({});
export type GenerateDraftRequest = z.infer<typeof generateDraftRequestSchema>;

export const feedbackDraftRequestSchema = z.object({
  feedback: z.string().min(1),
});
export type FeedbackDraftRequest = z.infer<typeof feedbackDraftRequestSchema>;

export const updateDraftRequestSchema = z.object({
  draft: z.string(),
});
export type UpdateDraftRequest = z.infer<typeof updateDraftRequestSchema>;

export const sendDraftRequestSchema = z.object({
  body: z.string().min(1),
});
export type SendDraftRequest = z.infer<typeof sendDraftRequestSchema>;
