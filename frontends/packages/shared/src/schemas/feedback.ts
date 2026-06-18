import { z } from 'zod';

export const feedbackStatusSchema = z.enum(['open', 'done']);
export type FeedbackStatus = z.infer<typeof feedbackStatusSchema>;

export const feedbackItemSchema = z.object({
  id: z.number().int().positive(),
  authAccountId: z.number().int().nonnegative(),
  authorEmail: z.string().nullable(),
  scope: z.string(),
  body: z.string(),
  status: feedbackStatusSchema,
  doneAt: z.string().datetime().nullable(),
  createdAt: z.string().datetime(),
});
export type FeedbackItem = z.infer<typeof feedbackItemSchema>;

export const createFeedbackSchema = z.object({
  body: z.string().min(1).max(4000),
  scope: z.string().min(1).max(40).default('general'),
});

export const updateFeedbackStatusSchema = z.object({
  status: feedbackStatusSchema,
});
