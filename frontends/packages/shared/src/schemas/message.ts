import { z } from 'zod';

export const attachmentKindSchema = z.enum(['image', 'video', 'audio', 'file']);
export type AttachmentKind = z.infer<typeof attachmentKindSchema>;

export const dmAttachmentSchema = z.object({
  kind: attachmentKindSchema,
  url: z.string().url(),
  thumbnailUrl: z.string().url().nullable(),
  fullUrl: z.string().url().nullable(),
  filename: z.string(),
  contentType: z.string(),
  byteSize: z.number().int().nullable(),
  width: z.number().int().nullable(),
  height: z.number().int().nullable(),
  voiceMessage: z.boolean(),
});
export type DmAttachment = z.infer<typeof dmAttachmentSchema>;

export const voiceTranscriptStatusSchema = z.enum(['pending', 'done', 'error']);
export type VoiceTranscriptStatus = z.infer<typeof voiceTranscriptStatusSchema>;

export const imageDescriptionStatusSchema = z.enum(['pending', 'done', 'error']);
export type ImageDescriptionStatus = z.infer<typeof imageDescriptionStatusSchema>;

export const dmImageDescriptionSchema = z.object({
  id: z.number().int().positive(),
  attachmentIndex: z.number().int().nonnegative(),
  description: z.string().nullable(),
  status: imageDescriptionStatusSchema,
  error: z.string().nullable(),
});
export type DmImageDescription = z.infer<typeof dmImageDescriptionSchema>;

export const dmMessageSchema = z.object({
  id: z.number().int().positive(),
  threadId: z.number().int().positive(),
  circleMessageId: z.number().int(),
  body: z.string(),
  senderId: z.number().int().nullable(),
  senderName: z.string().nullable(),
  senderIsMe: z.boolean(),
  parentMessageId: z.number().int().nullable(),
  chatThreadId: z.number().int().nullable(),
  createdAt: z.string().datetime(),
  editedAt: z.string().datetime().nullable(),
  attachments: z.array(dmAttachmentSchema).default([]),
  voiceTranscript: z.string().nullable().default(null),
  voiceTranscriptStatus: voiceTranscriptStatusSchema.nullable().default(null),
  voiceTranscriptError: z.string().nullable().default(null),
  voiceDurationSec: z.number().int().nullable().default(null),
  imageDescriptions: z.array(dmImageDescriptionSchema).default([]),
});

export type DmMessage = z.infer<typeof dmMessageSchema>;

export const messageListResponseSchema = z.object({
  messages: z.array(dmMessageSchema),
  hasPrevious: z.boolean(),
  hasNext: z.boolean(),
});

export type MessageListResponse = z.infer<typeof messageListResponseSchema>;
