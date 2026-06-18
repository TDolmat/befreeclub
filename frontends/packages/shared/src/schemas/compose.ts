import { z } from 'zod';

export const composeDraftResultSchema = z.object({
  draft: z.string(),
});
export type ComposeDraftResult = z.infer<typeof composeDraftResultSchema>;

export const composeSendResultSchema = z.union([
  z.object({
    ok: z.literal(true),
    threadId: z.number().int().positive(),
    circleChatRoomUuid: z.string(),
  }),
  z.object({
    ok: z.literal(false),
    error: z.string(),
  }),
]);
export type ComposeSendResult = z.infer<typeof composeSendResultSchema>;
