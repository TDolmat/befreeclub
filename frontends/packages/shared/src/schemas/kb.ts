import { z } from 'zod';

export const kbScopeSchema = z.enum(['global', 'account']);
export type KbScope = z.infer<typeof kbScopeSchema>;

export const kbSourceKindSchema = z.enum(['pdf', 'md', 'manual']);
export type KbSourceKind = z.infer<typeof kbSourceKindSchema>;

/** List item — no bodyText (can be large; fetched per-doc on demand). */
export const kbDocumentSchema = z.object({
  id: z.number().int().positive(),
  scope: kbScopeSchema,
  adminAccountId: z.number().int().positive().nullable(),
  title: z.string(),
  sourceKind: kbSourceKindSchema,
  originalFilename: z.string().nullable(),
  hasOriginal: z.boolean(),
  tokenEstimate: z.number().int(),
  enabled: z.boolean(),
  createdAt: z.string().datetime(),
  updatedAt: z.string().datetime(),
});
export type KbDocument = z.infer<typeof kbDocumentSchema>;

export const kbDocumentDetailSchema = kbDocumentSchema.extend({
  bodyText: z.string(),
});
export type KbDocumentDetail = z.infer<typeof kbDocumentDetailSchema>;

export const kbCapacitySchema = z.object({
  globalTokens: z.number().int(),
  accountTokens: z.number().int(),
  totalTokens: z.number().int(),
  budget: z.number().int(),
  hardCeiling: z.number().int(),
  overBudget: z.boolean(),
});
export type KbCapacity = z.infer<typeof kbCapacitySchema>;

export const kbListResponseSchema = z.object({
  documents: z.array(kbDocumentSchema),
  capacity: kbCapacitySchema,
});
export type KbListResponse = z.infer<typeof kbListResponseSchema>;

/** Create a manual (typed/pasted) entry. File uploads go via multipart. */
export const createKbManualSchema = z.object({
  scope: kbScopeSchema,
  adminAccountId: z.number().int().positive().nullable().optional(),
  title: z.string().min(1).max(200),
  bodyText: z.string().min(1).max(500_000),
});
export type CreateKbManual = z.infer<typeof createKbManualSchema>;

export const updateKbSchema = z
  .object({
    title: z.string().min(1).max(200).optional(),
    bodyText: z.string().max(500_000).optional(),
    enabled: z.boolean().optional(),
  })
  .refine((v) => Object.values(v).some((x) => x !== undefined), {
    message: 'at least one field required',
  });
export type UpdateKb = z.infer<typeof updateKbSchema>;
