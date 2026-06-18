import { z } from 'zod';
import { draftStatusSchema, iterationKindSchema } from './draft.js';

/**
 * Server → client WS event union. Frontend dispatches on `type`.
 */
export const wsEventSchema = z.discriminatedUnion('type', [
  z.object({
    type: z.literal('threads:updated'),
    adminAccountId: z.number().int(),
    changedThreadIds: z.array(z.number().int()),
  }),
  z.object({
    type: z.literal('thread:new_messages'),
    threadId: z.number().int(),
    newCount: z.number().int(),
  }),
  z.object({
    type: z.literal('messages:loaded'),
    threadId: z.number().int(),
    count: z.number().int(),
  }),
  z.object({
    type: z.literal('message:transcript_ready'),
    threadId: z.number().int(),
    messageId: z.number().int(),
  }),
  z.object({
    type: z.literal('message:image_description_ready'),
    threadId: z.number().int(),
    messageId: z.number().int(),
  }),
  z.object({
    type: z.literal('draft:status'),
    threadId: z.number().int(),
    status: draftStatusSchema,
    error: z.string().optional(),
  }),
  z.object({
    type: z.literal('draft:token'),
    threadId: z.number().int(),
    chunk: z.string(),
    iterationKind: iterationKindSchema,
  }),
  z.object({
    type: z.literal('draft:complete'),
    threadId: z.number().int(),
    iterationKind: iterationKindSchema,
    draft: z.string(),
    tokensUsed: z.number().int().nullable(),
    costUsd: z.number().nullable(),
  }),
  z.object({
    type: z.literal('draft:tool_use'),
    threadId: z.number().int(),
    toolName: z.string(),
  }),
  z.object({
    type: z.literal('send:result'),
    threadId: z.number().int(),
    ok: z.boolean(),
    circleMessageId: z.number().int().nullable(),
    error: z.string().optional(),
  }),
  z.object({
    type: z.literal('assistant:token'),
    conversationId: z.number().int(),
    chunk: z.string(),
  }),
  z.object({
    type: z.literal('assistant:complete'),
    conversationId: z.number().int(),
    messageId: z.number().int(),
    hasAction: z.boolean(),
  }),
  z.object({
    type: z.literal('assistant:error'),
    conversationId: z.number().int(),
    error: z.string(),
  }),
]);

export type WsEvent = z.infer<typeof wsEventSchema>;
export type WsEventType = WsEvent['type'];
