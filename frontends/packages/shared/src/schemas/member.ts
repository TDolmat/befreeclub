import { z } from 'zod';

export const communityMemberSchema = z.object({
  id: z.number().int().positive(),
  adminAccountId: z.number().int().positive(),
  circleCommunityMemberId: z.number().int().positive(),
  name: z.string(),
  email: z.string().nullable(),
  avatarUrl: z.string().nullable(),
  headline: z.string().nullable(),
  bio: z.string().nullable(),
  location: z.string().nullable(),
  lastSeenText: z.string().nullable(),
  status: z.string().nullable(),
  isAdmin: z.boolean(),
  canSendMessage: z.boolean(),
  fetchedAt: z.string().datetime(),
});

export type CommunityMember = z.infer<typeof communityMemberSchema>;

export const memberListResponseSchema = z.object({
  members: z.array(communityMemberSchema),
  count: z.number().int(),
});

export type MemberListResponse = z.infer<typeof memberListResponseSchema>;
