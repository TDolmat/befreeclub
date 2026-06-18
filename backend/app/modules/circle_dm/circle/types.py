"""Port circle/types.ts - ksztalty odpowiedzi Circle.so Headless Member API.

Wszystkie pola snake_case, dokladnie jak zwraca Circle. Runtime niczego nie
waliduje (klient zwraca surowe dicty z json.loads) - TypedDicty sa tylko
adnotacjami dla warstw wyzej.
"""

from typing import Any, Literal, NotRequired, TypedDict


class CircleAuthResponse(TypedDict):
    access_token: str
    refresh_token: str
    access_token_expires_at: str
    refresh_token_expires_at: str
    community_id: int
    community_member_id: int


class CircleParticipantPreview(TypedDict):
    id: int
    community_member_id: int
    name: str
    email: NotRequired[str]
    avatar_url: NotRequired[str]
    status: NotRequired[str]
    last_seen_text: NotRequired[str]


class CircleLastMessage(TypedDict):
    id: int
    body: str
    created_at: str
    sender: NotRequired[CircleParticipantPreview]
    rich_text_body: NotRequired[Any]


class CircleThreadRecord(TypedDict):
    id: int
    uuid: str
    identifier: NotRequired[str]
    chat_room_kind: Literal["direct", "group_chat"]
    chat_room_name: str | None
    unread_messages_count: int
    pinned_at: str | None
    chat_room_participants_count: int
    other_participants_preview: list[CircleParticipantPreview]
    current_participant: NotRequired[CircleParticipantPreview]
    last_message: CircleLastMessage | None


class CirclePaginatedThreads(TypedDict):
    records: list[CircleThreadRecord]
    page: int
    per_page: int
    has_next_page: bool
    count: int
    page_count: int


class CircleMessageRecord(TypedDict):
    id: int
    body: str
    rich_text_body: NotRequired[Any]
    created_at: str
    edited_at: str | None
    sent_at: NotRequired[str]
    parent_message_id: int | None
    chat_thread_id: int | None
    chat_room_uuid: str
    chat_room_participant_id: int
    sender: CircleParticipantPreview
    reactions: list[Any]
    bookmark_id: int | None


class CirclePaginatedMessages(TypedDict):
    records: list[CircleMessageRecord]
    first_id: int | None
    last_id: int | None
    total_count: int
    has_previous_page: bool
    has_next_page: bool


class CircleSendMessageResponse(TypedDict):
    """POST /chat_room_messages zwraca creation_uuid, NIE numeryczne id."""

    creation_uuid: str
    parent_message_id: int | None
    sent_at: str
    id: NotRequired[int]


class CircleFindOrCreateResponse(TypedDict):
    chat_room: CircleThreadRecord


class CircleMemberRoles(TypedDict):
    admin: NotRequired[bool]
    moderator: NotRequired[bool]


class CircleCommunityMemberRecord(TypedDict):
    """UWAGA: rekord membera NIE ma pola id - klucz to community_member_id."""

    community_member_id: int
    contact_id: NotRequired[int]
    name: str
    email: NotRequired[str]
    avatar_url: NotRequired[str]
    headline: NotRequired[str]
    bio: NotRequired[str]
    location: NotRequired[str]
    last_seen_text: NotRequired[str]
    status: NotRequired[str]
    user_id: NotRequired[int]
    roles: NotRequired[CircleMemberRoles]
    member_tags: NotRequired[list[str] | None]
    time_zone: NotRequired[str | None]


class CirclePaginatedCommunityMembers(TypedDict):
    records: list[CircleCommunityMemberRecord]
    page: int
    per_page: int
    has_next_page: bool
    count: int
    page_count: int
