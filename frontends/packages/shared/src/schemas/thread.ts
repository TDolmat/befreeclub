import { z } from 'zod';

export const chatRoomKindSchema = z.enum(['direct', 'group_chat']);
export type ChatRoomKind = z.infer<typeof chatRoomKindSchema>;

export const threadStatusSchema = z.enum(['inbox', 'done']);
export type ThreadStatus = z.infer<typeof threadStatusSchema>;

export const threadFilterSchema = z.enum([
  'inbox',
  'unread',
  'no_reply',
  'silent',
  'flagged',
  'checkup',
  'done',
]);
export type ThreadFilter = z.infer<typeof threadFilterSchema>;

export const threadSortSchema = z.enum(['recent', 'oldest_no_reply', 'next_checkup']);
export type ThreadSort = z.infer<typeof threadSortSchema>;

export const threadCheckupSchema = z.object({
  id: z.number().int().positive(),
  threadId: z.number().int().positive(),
  dueAt: z.string().datetime(),
  note: z.string().nullable(),
  doneAt: z.string().datetime().nullable(),
  createdAt: z.string().datetime(),
});
export type ThreadCheckup = z.infer<typeof threadCheckupSchema>;

export const dmThreadSchema = z.object({
  id: z.number().int().positive(),
  adminAccountId: z.number().int().positive(),
  circleChatRoomId: z.number().int(),
  circleChatRoomUuid: z.string(),
  chatRoomKind: chatRoomKindSchema,
  chatRoomName: z.string().nullable(),
  otherParticipantEmail: z.string().nullable(),
  otherParticipantName: z.string().nullable(),
  otherParticipantId: z.number().int().nullable(),
  otherParticipantAvatarUrl: z.string().nullable(),
  unreadMessagesCount: z.number().int().min(0),
  pinnedAt: z.string().datetime().nullable(),
  status: threadStatusSchema,
  isFlagged: z.boolean(),
  // Computed: next pending check-up (if any). Surfaces in the card without a
  // separate request.
  nextCheckupDueAt: z.string().datetime().nullable(),
  nextCheckupNote: z.string().nullable(),
  pendingCheckupCount: z.number().int().min(0),
  lastMessageAt: z.string().datetime().nullable(),
  lastMessageSenderId: z.number().int().nullable(),
  lastMessageSenderIsMe: z.boolean(),
  lastMessagePreview: z.string().nullable(),
  fetchedAt: z.string().datetime(),
});

export type DmThread = z.infer<typeof dmThreadSchema>;

export const threadListResponseSchema = z.object({
  threads: z.array(dmThreadSchema),
  count: z.number().int(),
});

export type ThreadListResponse = z.infer<typeof threadListResponseSchema>;
