import { z } from 'zod';

export const adminAccountSchema = z.object({
  id: z.number().int().positive(),
  label: z.string().min(1).max(120),
  email: z.string().email(),
  hasToken: z.boolean(),
  communityId: z.number().int().nullable(),
  communityMemberId: z.number().int().nullable(),
  systemPrompt: z.string(),
  isActive: z.boolean(),
  lastSyncedAt: z.string().datetime().nullable(),
  createdAt: z.string().datetime(),
  updatedAt: z.string().datetime(),
});

export type AdminAccount = z.infer<typeof adminAccountSchema>;

export const createAdminAccountSchema = z.object({
  label: z.string().min(1).max(120),
  email: z.string().email(),
  circleAdminToken: z.string().min(8).optional(),
  systemPrompt: z.string().min(10),
});

export type CreateAdminAccount = z.infer<typeof createAdminAccountSchema>;

export const updateAdminAccountSchema = createAdminAccountSchema
  .partial()
  .extend({ isActive: z.boolean().optional() });

export type UpdateAdminAccount = z.infer<typeof updateAdminAccountSchema>;

export const testConnectionResultSchema = z.object({
  ok: z.boolean(),
  communityId: z.number().int().nullable(),
  communityMemberId: z.number().int().nullable(),
  error: z.string().optional(),
});

export type TestConnectionResult = z.infer<typeof testConnectionResultSchema>;
