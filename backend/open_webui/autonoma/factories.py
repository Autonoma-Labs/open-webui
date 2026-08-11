"""Autonoma Environment Factory definitions.

One factory per model Autonoma can seed. Every ``create`` calls the same
data-access function the application itself uses, so the real business logic
(password hashing, access-grant wiring, version snapshots, channel
memberships, message dual-writes) runs for seeded rows exactly as it does for
rows a user creates through the UI.

A few conventions used throughout:

* **Ids are not seeded.** Most creation functions mint their own UUID, so the
  recipe links rows with ``_alias`` / ``_ref`` and the SDK substitutes the real
  id. Only models whose creation form *requires* a caller-supplied id
  (``File``, ``Model``, ``Tool``, ``Skill``, ``Function``) take one.
* **Time-sensitive columns take offsets, not instants.** A recipe is stored
  once and replayed for months, so any column the application compares against
  the current time is expressed as ``*_minutes_ago`` / ``*_in_minutes`` and
  resolved against the clock at seeding time. See ``_ago_s`` / ``_ahead_ns``.
* **Units differ per table.** ``chat``/``file``/``note``/``memory`` store epoch
  *seconds*; ``message``, ``calendar_event``, ``automation`` and
  ``message_reaction`` store epoch *nanoseconds*. The helpers below make the
  unit explicit at every call site.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from autonoma.factory import define_factory
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from open_webui.internal.db import get_async_db_context
from open_webui.models.access_grants import AccessGrants
from open_webui.models.auths import Auths
from open_webui.models.automations import AutomationForm, AutomationRuns, Automations
from open_webui.models.calendar import (
    CalendarEventAttendees,
    CalendarEventForm,
    CalendarEvents,
    CalendarForm,
    Calendars,
)
from open_webui.models.channels import Channels, ChannelWebhookForm, CreateChannelForm
from open_webui.models.chat_messages import ChatMessages
from open_webui.models.chats import ChatForm, Chats
from open_webui.models.config import Config
from open_webui.models.feedbacks import FeedbackForm, Feedbacks
from open_webui.models.files import FileForm, Files
from open_webui.models.folders import FolderForm, Folders
from open_webui.models.functions import FunctionForm, Functions
from open_webui.models.groups import GroupForm, Groups
from open_webui.models.knowledge import KnowledgeForm, Knowledges
from open_webui.models.memories import Memories
from open_webui.models.messages import MessageForm, Messages
from open_webui.models.models import ModelForm, Models
from open_webui.models.notes import NoteForm, Notes, PinnedNote
from open_webui.models.oauth_sessions import OAuthSessions
from open_webui.models.prompt_history import PromptHistories
from open_webui.models.prompts import PromptForm, Prompts
from open_webui.models.shared_chats import SharedChats
from open_webui.models.skills import SkillForm, Skills
from open_webui.models.tags import Tags
from open_webui.models.tools import ToolForm, Tools
from open_webui.models.users import Users
from open_webui.utils.auth import get_password_hash
from open_webui.utils.automations import next_run_ns

# The password every seeded account is created with. The auth callback hands
# this to the test runner alongside the account's email so a test can drive the
# real /auth login screen; it is hashed through the app's own hasher below.
SEEDED_PASSWORD = 'AutonomaTest123!'  # noqa: S105 - test-data password, not a credential


class _Input(BaseModel):
    """Base for factory inputs.

    ``extra='ignore'`` lets a recipe row carry ``_alias`` (and any other
    display-only key) without failing validation.
    """

    model_config = ConfigDict(extra='ignore')


# ---------------------------------------------------------------------------
# time helpers
#
# A factory runs at seeding time, so "now" here is the clock the test will run
# against. Offsets are resolved against it rather than baked into the recipe.
# ---------------------------------------------------------------------------


def _ago_s(minutes: int | None) -> int:
    """Epoch seconds ``minutes`` in the past (now when None)."""
    return int(time.time()) - int((minutes or 0) * 60)


def _ago_ns(minutes: int | None) -> int:
    """Epoch nanoseconds ``minutes`` in the past (now when None)."""
    return int(time.time_ns()) - int((minutes or 0) * 60 * 1_000_000_000)


def _ahead_ns(minutes: int | None, anchor_hour: int | None = None, anchor_minute: int = 0) -> int:
    """Epoch nanoseconds ``minutes`` in the future.

    When ``anchor_hour`` is given the result is moved to that local wall-clock
    time on the day the offset lands on, so a recipe can ask for "tomorrow at
    10:00" without naming a calendar date. The offset still decides the day, so
    the value stays correct however long the recipe is replayed for.
    """
    import datetime as dt

    target = dt.datetime.now().astimezone() + dt.timedelta(minutes=int(minutes or 0))
    if anchor_hour is not None:
        target = target.replace(hour=int(anchor_hour), minute=int(anchor_minute), second=0, microsecond=0)
    return int(target.timestamp() * 1_000_000_000)


def _rec(model: Any, **extra: Any) -> dict[str, Any]:
    """Turn a returned Pydantic row into the ref dict the SDK stores.

    Refs are signed into the teardown token, so only JSON-safe scalars the
    teardown actually needs are kept.
    """
    if model is None:
        raise RuntimeError('creation function returned None')
    record: dict[str, Any] = {'id': model.id}
    record.update(extra)
    return record


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------


class UserInput(_Input):
    name: str
    email: str
    role: str = 'user'
    username: str | None = None
    profile_image_url: str = '/user.png'
    password: str = SEEDED_PASSWORD
    # `user.created_at` is shown in the admin list but nothing branches on it;
    # the offset exists only so seeded accounts read as pre-existing.
    created_minutes_ago: int | None = None


async def _create_user(data: UserInput, ctx: Any) -> dict[str, Any]:
    """Create a full local account.

    ``insert_new_auth`` is the app's own account-creation path: it writes the
    ``auth`` credential row and the matching ``user`` row in one transaction,
    hashing the password with the application's hasher. That is why the User
    factory owns both tables.
    """
    user = await Auths.insert_new_auth(
        email=data.email,
        password=await get_password_hash(data.password),
        name=data.name,
        profile_image_url=data.profile_image_url,
        role=data.role,
    )
    if user is None:
        raise RuntimeError(f'insert_new_auth returned None for {data.email}')

    updates: dict[str, Any] = {}
    if data.username:
        updates['username'] = data.username
    if data.created_minutes_ago:
        updates['created_at'] = _ago_s(data.created_minutes_ago)
    if updates:
        await Users.update_user_by_id(user.id, updates)

    # The plaintext password rides along in refs so the auth callback can hand
    # the runner real login credentials without re-deriving it.
    return {'id': user.id, 'user_id': user.id, 'email': data.email, 'role': data.role, 'password': data.password}


class AuthInput(_Input):
    name: str
    email: str
    role: str = 'user'
    password: str = SEEDED_PASSWORD


async def _create_auth(data: AuthInput, ctx: Any) -> dict[str, Any]:
    """Create a credential row (and its user) via the same real path.

    In Open WebUI a local credential cannot exist without its user - one call
    writes both - so this factory is the User factory's function seen from the
    ``auth`` table's side. The standard recipe seeds accounts as ``User`` and
    leaves ``Auth`` out, because the User factory already mints the credential.
    """
    user = await Auths.insert_new_auth(
        email=data.email,
        password=await get_password_hash(data.password),
        name=data.name,
        role=data.role,
    )
    if user is None:
        raise RuntimeError(f'insert_new_auth returned None for {data.email}')
    return {'id': user.id, 'email': data.email, 'password': data.password}


class ApiKeyInput(_Input):
    user_id: str
    # Must keep the `sk-` prefix: get_current_user routes tokens starting with
    # `sk-` to API-key authentication.
    api_key: str
    label: str | None = None


async def _create_api_key(data: ApiKeyInput, ctx: Any) -> dict[str, Any]:
    if not await Users.update_user_api_key_by_id(data.user_id, data.api_key):
        raise RuntimeError(f'update_user_api_key_by_id failed for {data.user_id}')
    # The row's id is derived from the owner by the app itself.
    return {'id': f'key_{data.user_id}', 'user_id': data.user_id, 'api_key': data.api_key}


# ---------------------------------------------------------------------------
# groups and access control
# ---------------------------------------------------------------------------


class GroupInput(_Input):
    user_id: str
    name: str
    description: str = ''
    permissions: dict | None = None
    data: dict | None = None


async def _create_group(data: GroupInput, ctx: Any) -> dict[str, Any]:
    group = await Groups.insert_new_group(
        data.user_id,
        GroupForm(
            name=data.name,
            description=data.description,
            permissions=data.permissions,
            data=data.data,
        ),
    )
    return _rec(group)


class GroupMemberInput(_Input):
    group_id: str
    user_id: str


async def _create_group_member(data: GroupMemberInput, ctx: Any) -> dict[str, Any]:
    # add_users_to_group appends and ignores duplicates, so seeding members one
    # row at a time is safe and matches how the admin UI adds them.
    if await Groups.add_users_to_group(data.group_id, [data.user_id]) is None:
        raise RuntimeError(f'add_users_to_group failed for group {data.group_id}')
    return {'id': f'{data.group_id}:{data.user_id}', 'group_id': data.group_id, 'user_id': data.user_id}


class AccessGrantInput(_Input):
    resource_type: str
    resource_id: str
    principal_type: str  # user | group | anyone
    principal_id: str  # user id, group id, or '*'
    permission: str  # read | write


async def _create_access_grant(data: AccessGrantInput, ctx: Any) -> dict[str, Any]:
    grant = await AccessGrants.grant_access(
        resource_type=data.resource_type,
        resource_id=data.resource_id,
        principal_type=data.principal_type,
        principal_id=data.principal_id,
        permission=data.permission,
    )
    return _rec(
        grant,
        resource_type=data.resource_type,
        resource_id=data.resource_id,
        principal_type=data.principal_type,
        principal_id=data.principal_id,
        permission=data.permission,
    )


async def _teardown_access_grant(record: dict[str, Any], ctx: Any) -> None:
    await AccessGrants.revoke_access(
        resource_type=record['resource_type'],
        resource_id=record['resource_id'],
        principal_type=record['principal_type'],
        principal_id=record['principal_id'],
        permission=record['permission'],
    )


# ---------------------------------------------------------------------------
# models, folders, chats
# ---------------------------------------------------------------------------


class ModelInput(_Input):
    id: str
    user_id: str
    name: str
    base_model_id: str | None = None
    params: dict = {}
    meta: dict = {}
    is_active: bool = True


async def _create_model(data: ModelInput, ctx: Any) -> dict[str, Any]:
    model = await Models.insert_new_model(
        ModelForm(
            id=data.id,
            name=data.name,
            base_model_id=data.base_model_id,
            meta=data.meta,
            params=data.params,
            is_active=data.is_active,
        ),
        data.user_id,
    )
    return _rec(model)


class FolderInput(_Input):
    user_id: str
    name: str
    parent_id: str | None = None
    data: dict | None = None


async def _create_folder(data: FolderInput, ctx: Any) -> dict[str, Any]:
    folder = await Folders.insert_new_folder(
        data.user_id,
        FolderForm(name=data.name, data=data.data),
        parent_id=data.parent_id,
    )
    return _rec(folder, user_id=data.user_id)


async def _teardown_folder(record: dict[str, Any], ctx: Any) -> None:
    await Folders.delete_folder_by_id_and_user_id(record['id'], record['user_id'])


class ChatMessageSpec(_Input):
    """One message inside a seeded chat's history."""

    id: str | None = None
    role: str
    content: str
    model: str | None = None


class ChatInput(_Input):
    user_id: str
    title: str
    folder_id: str | None = None
    models: list[str] = []
    messages: list[ChatMessageSpec] = []
    archived: bool = False
    pinned: bool = False
    # The sidebar buckets chats into Today / Yesterday / Previous 7 days from
    # `updated_at`, so both timestamps are offsets rather than fixed instants.
    created_minutes_ago: int | None = None
    updated_minutes_ago: int | None = None


async def _create_chat(data: ChatInput, ctx: Any) -> dict[str, Any]:
    chat_id = str(uuid.uuid4())

    # Build the chat JSON in the shape the app writes it: a linear history
    # under `history.messages` plus `currentId`. insert_new_chat dual-writes
    # every message here into the chat_message table.
    messages: dict[str, Any] = {}
    order: list[str] = []
    parent_id: str | None = None
    for spec in data.messages:
        message_id = spec.id or str(uuid.uuid4())
        messages[message_id] = {
            'id': message_id,
            'parentId': parent_id,
            'childrenIds': [],
            'role': spec.role,
            'content': spec.content,
            'model': spec.model,
            'timestamp': _ago_s(data.updated_minutes_ago),
            'done': True,
        }
        if parent_id is not None:
            messages[parent_id]['childrenIds'].append(message_id)
        order.append(message_id)
        parent_id = message_id

    chat_json: dict[str, Any] = {
        'title': data.title,
        'models': data.models,
        'messages': [messages[mid] for mid in order],
        'history': {'messages': messages, 'currentId': order[-1] if order else None},
    }

    chat = await Chats.insert_new_chat(
        chat_id,
        data.user_id,
        ChatForm(chat=chat_json, folder_id=data.folder_id),
    )
    if chat is None:
        raise RuntimeError(f'insert_new_chat returned None for {data.title!r}')

    # archived / pinned are toggles in the app, not insert columns.
    if data.archived:
        await Chats.toggle_chat_archive_by_id(chat_id)
    if data.pinned:
        await Chats.toggle_chat_pinned_by_id(chat_id)

    if data.created_minutes_ago is not None or data.updated_minutes_ago is not None:
        await _backdate_chat(chat_id, data.created_minutes_ago, data.updated_minutes_ago)

    return {'id': chat_id, 'user_id': data.user_id}


async def _teardown_chat(record: dict[str, Any], ctx: Any) -> None:
    """Delete a chat and everything hanging off it.

    `delete_chat_by_id` already cascades `chat_message` and the share snapshot,
    but not `chat_file`, so file attachments are cleared first - including any a
    test attached mid-run, which were never part of `up`.
    """
    chat_id = record['id']
    for file_id in await _chat_file_ids(chat_id):
        await Chats.delete_chat_file(chat_id, file_id)
    await Chats.delete_chat_by_id(chat_id)


async def _chat_file_ids(chat_id: str) -> list[str]:
    from open_webui.models.chats import ChatFile

    async with get_async_db_context() as session:
        result = await session.execute(select(ChatFile.file_id).filter_by(chat_id=chat_id))
        return [row[0] for row in result.all()]


async def _backdate_chat(chat_id: str, created_minutes_ago: int | None, updated_minutes_ago: int | None) -> None:
    """Move a seeded chat's timestamps into the past.

    ``insert_new_chat`` stamps ``now``; the sidebar's date buckets are what the
    tests read, so the offsets are applied to the row after creation. Written
    against the same ORM model the app uses.
    """
    from open_webui.models.chats import Chat

    async with get_async_db_context() as session:
        chat = await session.get(Chat, chat_id)
        if chat is None:
            return
        if created_minutes_ago is not None:
            chat.created_at = _ago_s(created_minutes_ago)
        if updated_minutes_ago is not None:
            chat.updated_at = _ago_s(updated_minutes_ago)
            chat.last_read_at = _ago_s(updated_minutes_ago)
        await session.commit()


class ChatMessageInput(_Input):
    chat_id: str
    user_id: str
    role: str
    content: str
    message_id: str | None = None
    model_id: str | None = None
    parent_id: str | None = None
    created_minutes_ago: int | None = None


async def _create_chat_message(data: ChatMessageInput, ctx: Any) -> dict[str, Any]:
    message_id = data.message_id or str(uuid.uuid4())
    payload: dict[str, Any] = {
        'role': data.role,
        'content': data.content,
        'model_id': data.model_id,
        'parent_id': data.parent_id,
        'done': True,
        'timestamp': _ago_s(data.created_minutes_ago),
    }
    row = await ChatMessages.upsert_message(
        message_id=message_id,
        chat_id=data.chat_id,
        user_id=data.user_id,
        data=payload,
    )
    if row is None:
        raise RuntimeError(f'upsert_message returned None for chat {data.chat_id}')
    # The app keys chat_message rows as `{chat_id}-{message_id}`.
    return {'id': row.id, 'chat_id': data.chat_id, 'message_id': message_id}


async def _teardown_chat_message(record: dict[str, Any], ctx: Any) -> None:
    await ChatMessages.delete_message_ids_by_chat_id(record['chat_id'], {record['message_id']})


class ChatFileInput(_Input):
    chat_id: str
    file_id: str
    user_id: str
    message_id: str


async def _create_chat_file(data: ChatFileInput, ctx: Any) -> dict[str, Any]:
    # insert_chat_files enforces that the caller can read the file before
    # linking it, which is exactly the check we want to keep exercising.
    rows = await Chats.insert_chat_files(
        chat_id=data.chat_id,
        message_id=data.message_id,
        file_ids=[data.file_id],
        user_id=data.user_id,
    )
    if not rows:
        raise RuntimeError(f'insert_chat_files linked nothing for file {data.file_id}')
    return {'id': rows[0].id, 'chat_id': data.chat_id, 'file_id': data.file_id}


async def _teardown_chat_file(record: dict[str, Any], ctx: Any) -> None:
    await Chats.delete_chat_file(record['chat_id'], record['file_id'])


class TagInput(_Input):
    name: str
    user_id: str
    chat_id: str | None = None


async def _create_tag(data: TagInput, ctx: Any) -> dict[str, Any]:
    tag = await Tags.insert_new_tag(data.name, data.user_id)
    if tag is None:
        raise RuntimeError(f'insert_new_tag returned None for {data.name!r}')
    if data.chat_id:
        # Chat tags live in chat.meta.tags; this is the app's own linker.
        await Chats.add_chat_tag_by_id_and_user_id_and_tag_name(data.chat_id, data.user_id, data.name)
    return {'id': tag.id, 'name': data.name, 'user_id': data.user_id, 'chat_id': data.chat_id}


async def _teardown_tag(record: dict[str, Any], ctx: Any) -> None:
    if record.get('chat_id'):
        await Chats.delete_tag_by_id_and_user_id_and_tag_name(record['chat_id'], record['user_id'], record['name'])
    await Tags.delete_tag_by_name_and_user_id(record['name'], record['user_id'])


class FeedbackInput(_Input):
    user_id: str
    type: str = 'rating'
    chat_id: str | None = None
    message_id: str | None = None
    rating: int | str | None = None
    comment: str | None = None
    model_id: str | None = None
    reason: str | None = None


async def _create_feedback(data: FeedbackInput, ctx: Any) -> dict[str, Any]:
    from open_webui.models.feedbacks import RatingData

    feedback = await Feedbacks.insert_new_feedback(
        data.user_id,
        FeedbackForm(
            type=data.type,
            data=RatingData(
                rating=data.rating,
                comment=data.comment,
                model_id=data.model_id,
                reason=data.reason,
            ),
            # get_feedbacks_by_chat_id reads meta.chat_id, so the references
            # belong in meta rather than as columns.
            meta={'chat_id': data.chat_id, 'message_id': data.message_id},
        ),
    )
    return _rec(feedback)


class SharedChatInput(_Input):
    chat_id: str
    user_id: str


async def _create_shared_chat(data: SharedChatInput, ctx: Any) -> dict[str, Any]:
    # Mirrors POST /chats/{id}/share: snapshot the chat, then point the chat at
    # the share so /s/{id} resolves. The endpoint's event publish is a request
    # side effect and is deliberately not reproduced.
    shared = await SharedChats.create(data.chat_id, data.user_id)
    if shared is None:
        raise RuntimeError(f'SharedChats.create returned None for chat {data.chat_id}')
    await Chats.update_chat_share_id_by_id(data.chat_id, shared.id)
    return {'id': shared.id, 'chat_id': data.chat_id}


async def _teardown_shared_chat(record: dict[str, Any], ctx: Any) -> None:
    await Chats.update_chat_share_id_by_id(record['chat_id'], None)
    await SharedChats.delete_by_id(record['id'])


# ---------------------------------------------------------------------------
# files and knowledge
# ---------------------------------------------------------------------------


class FileInput(_Input):
    id: str
    user_id: str
    filename: str
    path: str
    hash: str | None = None
    data: dict = {}
    meta: dict = {}


async def _create_file(data: FileInput, ctx: Any) -> dict[str, Any]:
    file = await Files.insert_new_file(
        data.user_id,
        FileForm(
            id=data.id,
            filename=data.filename,
            path=data.path,
            hash=data.hash,
            data=data.data,
            meta=data.meta,
        ),
    )
    return _rec(file)


class KnowledgeInput(_Input):
    user_id: str
    name: str
    description: str = ''


async def _create_knowledge(data: KnowledgeInput, ctx: Any) -> dict[str, Any]:
    knowledge = await Knowledges.insert_new_knowledge(
        data.user_id,
        KnowledgeForm(name=data.name, description=data.description),
    )
    return _rec(knowledge)


async def _teardown_knowledge(record: dict[str, Any], ctx: Any) -> None:
    """Delete a knowledge base and its whole contents.

    `delete_knowledge_by_id` drops the row and its grants but leaves
    `knowledge_file` / `knowledge_directory` behind, so reset first - that also
    clears files a test uploaded into the base mid-run.
    """
    await Knowledges.reset_knowledge_by_id(record['id'], include_directories=True)
    await Knowledges.delete_knowledge_by_id(record['id'])


class KnowledgeDirectoryInput(_Input):
    knowledge_id: str
    name: str
    user_id: str
    parent_id: str | None = None


async def _create_knowledge_directory(data: KnowledgeDirectoryInput, ctx: Any) -> dict[str, Any]:
    directory = await Knowledges.create_directory(
        knowledge_id=data.knowledge_id,
        name=data.name,
        user_id=data.user_id,
        parent_id=data.parent_id,
    )
    return _rec(directory)


async def _teardown_knowledge_directory(record: dict[str, Any], ctx: Any) -> None:
    await Knowledges.delete_directory(record['id'])


class KnowledgeFileInput(_Input):
    knowledge_id: str
    file_id: str
    user_id: str
    directory_id: str | None = None


async def _create_knowledge_file(data: KnowledgeFileInput, ctx: Any) -> dict[str, Any]:
    link = await Knowledges.add_file_to_knowledge_by_id(
        knowledge_id=data.knowledge_id,
        file_id=data.file_id,
        user_id=data.user_id,
        directory_id=data.directory_id,
    )
    return _rec(link, knowledge_id=data.knowledge_id, file_id=data.file_id)


async def _teardown_knowledge_file(record: dict[str, Any], ctx: Any) -> None:
    await Knowledges.remove_file_from_knowledge_by_id(record['knowledge_id'], record['file_id'])


# ---------------------------------------------------------------------------
# channels
# ---------------------------------------------------------------------------


class ChannelInput(_Input):
    user_id: str
    name: str
    description: str | None = None
    type: str | None = None  # None/'' = open channel, 'group' or 'dm' = membership-scoped
    user_ids: list[str] | None = None
    group_ids: list[str] | None = None


async def _create_channel(data: ChannelInput, ctx: Any) -> dict[str, Any]:
    channel = await Channels.insert_new_channel(
        CreateChannelForm(
            name=data.name,
            description=data.description,
            type=data.type,
            user_ids=data.user_ids,
            group_ids=data.group_ids,
        ),
        data.user_id,
    )
    return _rec(channel)


async def _teardown_channel(record: dict[str, Any], ctx: Any) -> None:
    """Delete a channel and everything under it.

    `delete_channel_by_id` removes the channel row and its access grants but
    leaves memberships, files, webhooks and messages behind. Tearing the whole
    subtree down here matters for more than tidiness: posting a message joins
    its author to the channel as a side effect, so a channel accumulates
    membership rows that were never in `up` and that per-record teardown would
    leak. Deleting by the scoping root also removes anything a test itself
    posted mid-run.
    """
    channel_id = record['id']

    for message_id in await _channel_message_ids(channel_id):
        # Cascades the message's reactions.
        await Messages.delete_message_by_id(message_id)

    for file_id in await _channel_file_ids(channel_id):
        await Channels.remove_file_from_channel_by_id(channel_id, file_id)

    for webhook in await Channels.get_webhooks_by_channel_id(channel_id):
        await Channels.delete_webhook_by_id(webhook.id)

    members = await Channels.get_members_by_channel_id(channel_id)
    if members:
        await Channels.remove_members_from_channel(channel_id, [m.user_id for m in members])

    await Channels.delete_channel_by_id(channel_id)


async def _channel_message_ids(channel_id: str) -> list[str]:
    """Every message in a channel, thread replies included."""
    from open_webui.models.messages import Message

    async with get_async_db_context() as session:
        result = await session.execute(select(Message.id).filter_by(channel_id=channel_id))
        return [row[0] for row in result.all()]


async def _channel_file_ids(channel_id: str) -> list[str]:
    from open_webui.models.channels import ChannelFile

    async with get_async_db_context() as session:
        result = await session.execute(select(ChannelFile.file_id).filter_by(channel_id=channel_id))
        return [row[0] for row in result.all()]


class ChannelMemberInput(_Input):
    channel_id: str
    user_id: str


async def _create_channel_member(data: ChannelMemberInput, ctx: Any) -> dict[str, Any]:
    member = await Channels.join_channel(data.channel_id, data.user_id)
    return _rec(member, channel_id=data.channel_id, user_id=data.user_id)


async def _teardown_channel_member(record: dict[str, Any], ctx: Any) -> None:
    await Channels.remove_members_from_channel(record['channel_id'], [record['user_id']])


class ChannelFileInput(_Input):
    channel_id: str
    file_id: str
    user_id: str


async def _create_channel_file(data: ChannelFileInput, ctx: Any) -> dict[str, Any]:
    link = await Channels.add_file_to_channel_by_id(data.channel_id, data.file_id, data.user_id)
    return _rec(link, channel_id=data.channel_id, file_id=data.file_id)


async def _teardown_channel_file(record: dict[str, Any], ctx: Any) -> None:
    await Channels.remove_file_from_channel_by_id(record['channel_id'], record['file_id'])


class ChannelWebhookInput(_Input):
    channel_id: str
    user_id: str
    name: str
    profile_image_url: str | None = None


async def _create_channel_webhook(data: ChannelWebhookInput, ctx: Any) -> dict[str, Any]:
    # The webhook's URL/token is minted server-side; only the display fields
    # are caller-supplied.
    webhook = await Channels.insert_webhook(
        channel_id=data.channel_id,
        user_id=data.user_id,
        form_data=ChannelWebhookForm(name=data.name, profile_image_url=data.profile_image_url),
    )
    return _rec(webhook)


async def _teardown_channel_webhook(record: dict[str, Any], ctx: Any) -> None:
    await Channels.delete_webhook_by_id(record['id'])


class MessageInput(_Input):
    channel_id: str
    user_id: str
    content: str
    parent_id: str | None = None
    reply_to_id: str | None = None
    data: dict | None = None
    meta: dict | None = None
    created_minutes_ago: int | None = None


async def _create_message(data: MessageInput, ctx: Any) -> dict[str, Any]:
    # insert_new_message joins the author to the channel as a side effect,
    # which is how a real post behaves.
    message = await Messages.insert_new_message(
        MessageForm(
            content=data.content,
            parent_id=data.parent_id,
            reply_to_id=data.reply_to_id,
            data=data.data,
            meta=data.meta,
        ),
        data.channel_id,
        data.user_id,
    )
    if message is None:
        raise RuntimeError(f'insert_new_message returned None for channel {data.channel_id}')

    if data.created_minutes_ago:
        await _backdate_message(message.id, data.created_minutes_ago)

    return {'id': message.id, 'channel_id': data.channel_id}


async def _backdate_message(message_id: str, minutes_ago: int) -> None:
    """Place a channel message earlier in the thread's ordering."""
    from open_webui.models.messages import Message

    async with get_async_db_context() as session:
        row = await session.get(Message, message_id)
        if row is None:
            return
        row.created_at = _ago_ns(minutes_ago)
        row.updated_at = _ago_ns(minutes_ago)
        await session.commit()


class MessageReactionInput(_Input):
    message_id: str
    user_id: str
    name: str


async def _create_message_reaction(data: MessageReactionInput, ctx: Any) -> dict[str, Any]:
    reaction = await Messages.add_reaction_to_message(data.message_id, data.user_id, data.name)
    return _rec(reaction, message_id=data.message_id, user_id=data.user_id, name=data.name)


async def _teardown_message_reaction(record: dict[str, Any], ctx: Any) -> None:
    await Messages.remove_reaction_by_id_and_user_id_and_name(record['message_id'], record['user_id'], record['name'])


# ---------------------------------------------------------------------------
# automations
# ---------------------------------------------------------------------------


class AutomationInput(_Input):
    user_id: str
    name: str
    prompt: str
    model_id: str
    rrule: str
    is_active: bool = True
    folder_id: str | None = None
    meta: dict | None = None


async def _create_automation(data: AutomationInput, ctx: Any) -> dict[str, Any]:
    from open_webui.models.automations import AutomationData

    form = AutomationForm(
        name=data.name,
        folder_id=data.folder_id,
        data=AutomationData(prompt=data.prompt, model_id=data.model_id, rrule=data.rrule),
        meta=data.meta,
        is_active=data.is_active,
    )
    # next_run_at is derived from the rrule by the app's own scheduler helper,
    # exactly as POST /automations does. That keeps the row's next run in the
    # future however long after seeding the test runs - a past value would make
    # the scheduler fire the automation mid-test.
    automation = await Automations.insert(data.user_id, form, next_run_ns(data.rrule))
    return _rec(automation)


async def _teardown_automation(record: dict[str, Any], ctx: Any) -> None:
    await AutomationRuns.delete_by_automation(record['id'])
    await Automations.delete(record['id'])


class AutomationRunInput(_Input):
    automation_id: str
    status: str  # success | error
    chat_id: str | None = None
    error: str | None = None
    # Ordering matters: get_latest picks the highest created_at as "last run".
    created_minutes_ago: int | None = None


async def _create_automation_run(data: AutomationRunInput, ctx: Any) -> dict[str, Any]:
    run = await AutomationRuns.insert(
        automation_id=data.automation_id,
        chat_id=data.chat_id,
        status=data.status,
        error=data.error,
    )
    if run is None:
        raise RuntimeError(f'AutomationRuns.insert returned None for {data.automation_id}')
    if data.created_minutes_ago:
        await _backdate_automation_run(run.id, data.created_minutes_ago)
    return {'id': run.id, 'automation_id': data.automation_id}


async def _backdate_automation_run(run_id: str, minutes_ago: int) -> None:
    from open_webui.models.automations import AutomationRun

    async with get_async_db_context() as session:
        row = await session.get(AutomationRun, run_id)
        if row is None:
            return
        row.created_at = _ago_ns(minutes_ago)
        await session.commit()


async def _teardown_automation_run(record: dict[str, Any], ctx: Any) -> None:
    # Scoped to a test-seeded automation, so removing its runs is test-only.
    await AutomationRuns.delete_by_automation(record['automation_id'])


# ---------------------------------------------------------------------------
# calendar
# ---------------------------------------------------------------------------


class CalendarInput(_Input):
    user_id: str
    name: str
    color: str | None = None


async def _create_calendar(data: CalendarInput, ctx: Any) -> dict[str, Any]:
    calendar = await Calendars.insert_new_calendar(
        data.user_id,
        CalendarForm(name=data.name, color=data.color),
    )
    return _rec(calendar)


async def _teardown_calendar(record: dict[str, Any], ctx: Any) -> None:
    await AccessGrants.revoke_all_access('calendar', record['id'])
    await Calendars.delete_calendar_by_id(record['id'])


class CalendarEventInput(_Input):
    calendar_id: str
    user_id: str
    title: str
    # start/end are epoch-ns columns the calendar range query and the upcoming
    # -event reminder both compare against now, so they are offsets. Negative
    # `starts_in_minutes` puts the event in the past.
    starts_in_minutes: int
    duration_minutes: int = 60
    anchor_hour: int | None = None
    anchor_minute: int = 0
    description: str | None = None
    location: str | None = None
    all_day: bool = False
    rrule: str | None = None
    color: str | None = None


async def _create_calendar_event(data: CalendarEventInput, ctx: Any) -> dict[str, Any]:
    start_at = _ahead_ns(data.starts_in_minutes, data.anchor_hour, data.anchor_minute)
    end_at = start_at + int(data.duration_minutes * 60 * 1_000_000_000)
    event = await CalendarEvents.insert_new_event(
        data.user_id,
        CalendarEventForm(
            calendar_id=data.calendar_id,
            title=data.title,
            description=data.description,
            start_at=start_at,
            end_at=end_at,
            all_day=data.all_day,
            rrule=data.rrule,
            location=data.location,
            color=data.color,
        ),
    )
    return _rec(event)


class CalendarEventAttendeeInput(_Input):
    event_id: str
    user_id: str
    meta: dict | None = None


async def _create_calendar_event_attendee(data: CalendarEventAttendeeInput, ctx: Any) -> dict[str, Any]:
    # set_attendees REPLACES an event's attendee list, so seeding row by row
    # means re-sending the ones already there plus this one.
    existing = await CalendarEventAttendees.get_attendees_by_event(data.event_id)
    attendees = [{'user_id': a.user_id, 'meta': a.meta} for a in existing if a.user_id != data.user_id]
    attendees.append({'user_id': data.user_id, 'meta': data.meta})

    rows = await CalendarEventAttendees.set_attendees(data.event_id, attendees)
    if not any(r.user_id == data.user_id for r in rows):
        raise RuntimeError(f'set_attendees did not create attendee {data.user_id}')
    # Identify the row by (event_id, user_id) - the table's own unique key -
    # rather than by its surrogate id: set_attendees deletes and re-inserts the
    # whole list, so a stored uuid would go stale as soon as the next attendee
    # for the same event is seeded.
    return {'id': f'{data.event_id}:{data.user_id}', 'event_id': data.event_id, 'user_id': data.user_id}


async def _teardown_calendar_event_attendee(record: dict[str, Any], ctx: Any) -> None:
    remaining = [
        {'user_id': a.user_id, 'meta': a.meta}
        for a in await CalendarEventAttendees.get_attendees_by_event(record['event_id'])
        if a.user_id != record['user_id']
    ]
    await CalendarEventAttendees.set_attendees(record['event_id'], remaining)


# ---------------------------------------------------------------------------
# prompts, notes, workspace extensions
# ---------------------------------------------------------------------------


class PromptInput(_Input):
    user_id: str
    command: str
    name: str
    content: str
    tags: list[str] = []
    commit_message: str | None = None


async def _create_prompt(data: PromptInput, ctx: Any) -> dict[str, Any]:
    prompt = await Prompts.insert_new_prompt(
        data.user_id,
        PromptForm(
            command=data.command,
            name=data.name,
            content=data.content,
            tags=data.tags,
            commit_message=data.commit_message,
        ),
    )
    if prompt is None or prompt.id is None:
        raise RuntimeError(f'insert_new_prompt returned None for {data.command!r}')
    return {'id': prompt.id, 'command': data.command}


async def _teardown_prompt(record: dict[str, Any], ctx: Any) -> None:
    await AccessGrants.revoke_all_access('prompt', record['id'])
    await PromptHistories.delete_history_by_prompt_id(record['id'])
    await Prompts.delete_prompt_by_id(record['id'])


class PromptHistoryInput(_Input):
    prompt_id: str
    user_id: str
    name: str
    content: str
    command: str
    commit_message: str | None = None


async def _create_prompt_history(data: PromptHistoryInput, ctx: Any) -> dict[str, Any]:
    # A prompt already has an initial version from insert_new_prompt; this adds
    # a later revision, snapshot-shaped the way the app writes one.
    entry = await PromptHistories.create_history_entry(
        prompt_id=data.prompt_id,
        snapshot={
            'name': data.name,
            'content': data.content,
            'command': data.command,
            'data': {},
            'meta': {},
            'tags': [],
            'access_grants': [],
        },
        user_id=data.user_id,
        commit_message=data.commit_message,
    )
    return _rec(entry, prompt_id=data.prompt_id)


async def _teardown_prompt_history(record: dict[str, Any], ctx: Any) -> None:
    await PromptHistories.delete_history_entry(record['id'], record['prompt_id'])


class NoteInput(_Input):
    user_id: str
    title: str
    content: str = ''


async def _create_note(data: NoteInput, ctx: Any) -> dict[str, Any]:
    note = await Notes.insert_new_note(
        data.user_id,
        # The editor stores its document under data.content.
        NoteForm(title=data.title, data={'content': {'md': data.content}}),
    )
    return _rec(note)


async def _teardown_note(record: dict[str, Any], ctx: Any) -> None:
    await AccessGrants.revoke_all_access('note', record['id'])
    await Notes.delete_note_by_id(record['id'])


class PinnedNoteInput(_Input):
    note_id: str
    user_id: str


async def _create_pinned_note(data: PinnedNoteInput, ctx: Any) -> dict[str, Any]:
    if await Notes.toggle_note_pinned_by_id(data.note_id, data.user_id) is None:
        raise RuntimeError(f'toggle_note_pinned_by_id failed for note {data.note_id}')
    pin_id = await _pinned_note_id(data.note_id, data.user_id)
    if pin_id is None:
        raise RuntimeError(f'note {data.note_id} was not pinned for {data.user_id}')
    return {'id': pin_id, 'note_id': data.note_id, 'user_id': data.user_id}


async def _pinned_note_id(note_id: str, user_id: str) -> str | None:
    async with get_async_db_context() as session:
        result = await session.execute(select(PinnedNote).filter_by(user_id=user_id, note_id=note_id))
        row = result.scalars().first()
        return row.id if row else None


async def _teardown_pinned_note(record: dict[str, Any], ctx: Any) -> None:
    # toggle_note_pinned_by_id would re-pin an already-unpinned note, so only
    # toggle while the pin is actually there. Keeps `down` idempotent.
    if await _pinned_note_id(record['note_id'], record['user_id']) is not None:
        await Notes.toggle_note_pinned_by_id(record['note_id'], record['user_id'])


class ToolInput(_Input):
    id: str
    user_id: str
    name: str
    content: str
    description: str | None = None


async def _create_tool(data: ToolInput, ctx: Any) -> dict[str, Any]:
    from open_webui.models.tools import ToolMeta

    specs = await _tool_specs(data.id, data.content)
    tool = await Tools.insert_new_tool(
        data.user_id,
        ToolForm(
            id=data.id,
            name=data.name,
            content=data.content,
            meta=ToolMeta(description=data.description),
        ),
        specs,
    )
    return _rec(tool)


async def _tool_specs(tool_id: str, content: str) -> list[dict]:
    """Derive a tool's specs the way POST /tools/create does.

    The router loads the tool module and introspects it. Seeded content is
    valid Python, so this normally produces real specs; if a module fails to
    load we fall back to an empty list rather than failing the whole seed.
    """
    try:
        from open_webui.utils.plugin import load_tool_module_by_id
        from open_webui.utils.tools import get_tool_specs

        tool_module, _ = await load_tool_module_by_id(tool_id, content=content)
        return get_tool_specs(tool_module)
    except Exception:
        return []


async def _teardown_tool(record: dict[str, Any], ctx: Any) -> None:
    await AccessGrants.revoke_all_access('tool', record['id'])
    await Tools.delete_tool_by_id(record['id'])


class SkillInput(_Input):
    id: str
    user_id: str
    name: str
    content: str
    description: str | None = None
    tags: list[str] = []
    is_active: bool = True


async def _create_skill(data: SkillInput, ctx: Any) -> dict[str, Any]:
    from open_webui.models.skills import SkillMeta

    skill = await Skills.insert_new_skill(
        data.user_id,
        SkillForm(
            id=data.id,
            name=data.name,
            description=data.description,
            content=data.content,
            meta=SkillMeta(tags=data.tags),
            is_active=data.is_active,
        ),
    )
    return _rec(skill)


async def _teardown_skill(record: dict[str, Any], ctx: Any) -> None:
    await AccessGrants.revoke_all_access('skill', record['id'])
    await Skills.delete_skill_by_id(record['id'])


class FunctionInput(_Input):
    id: str
    user_id: str
    name: str
    content: str
    type: str = 'filter'  # filter | pipe | action
    description: str | None = None


async def _create_function(data: FunctionInput, ctx: Any) -> dict[str, Any]:
    from open_webui.models.functions import FunctionMeta

    function = await Functions.insert_new_function(
        data.user_id,
        data.type,
        FunctionForm(
            id=data.id,
            name=data.name,
            content=data.content,
            meta=FunctionMeta(description=data.description),
        ),
    )
    return _rec(function)


class MemoryInput(_Input):
    user_id: str
    content: str
    memory_type: str | None = None
    path: str | None = None
    meta: dict | None = None


async def _create_memory(data: MemoryInput, ctx: Any) -> dict[str, Any]:
    # The memories router also writes an embedding to the vector store; that is
    # an external-service side effect of the request and is not reproduced
    # here. Every memory screen reads the relational row seeded below.
    memory = await Memories.insert_new_memory(
        data.user_id,
        data.content,
        memory_type=data.memory_type,
        path=data.path,
        meta=data.meta or {'created_by': 'autonoma'},
    )
    return _rec(memory)


class OAuthSessionInput(_Input):
    user_id: str
    provider: str
    # `expires_at` is an absolute epoch the app checks against now, so the
    # recipe supplies a lifetime and the factory anchors it at seeding time.
    expires_in_minutes: int = 720
    access_token: str | None = None


async def _create_oauth_session(data: OAuthSessionInput, ctx: Any) -> dict[str, Any]:
    token = {
        'access_token': data.access_token or f'autonoma-{uuid.uuid4().hex}',
        'token_type': 'Bearer',
        'expires_at': int(time.time()) + int(data.expires_in_minutes * 60),
    }
    session = await OAuthSessions.create_session(data.user_id, data.provider, token)
    return _rec(session)


async def _teardown_oauth_session(record: dict[str, Any], ctx: Any) -> None:
    await OAuthSessions.delete_session_by_id(record['id'])


class ConfigInput(_Input):
    key: str
    value: Any


async def _create_config(data: ConfigInput, ctx: Any) -> dict[str, Any]:
    """Set a global config key.

    The ``config`` table is keyed by the config key itself, so it is the one
    seeded table that cannot be made per-run - see IMPLEMENTATION.md. To stay
    non-destructive the previous value is captured and restored on teardown
    instead of the row being deleted outright.
    """
    previous = await Config.get(data.key)
    had_previous = previous is not None
    await Config.upsert({data.key: data.value})
    return {
        'id': data.key,
        'key': data.key,
        'previous_value': previous,
        'had_previous': had_previous,
    }


async def _teardown_config(record: dict[str, Any], ctx: Any) -> None:
    if record.get('had_previous'):
        await Config.upsert({record['key']: record['previous_value']})
    else:
        await Config.delete(record['key'])


# ---------------------------------------------------------------------------
# registry
#
# Teardown order is derived by the SDK from the recipe's _alias/_ref graph and
# runs in reverse, so children are removed before the rows they point at.
# ---------------------------------------------------------------------------

FACTORIES = {
    'User': define_factory(
        create=_create_user,
        teardown=lambda record, ctx: Auths.delete_auth_by_id(record['id']),
        input_model=UserInput,
    ),
    'Auth': define_factory(
        create=_create_auth,
        teardown=lambda record, ctx: Auths.delete_auth_by_id(record['id']),
        input_model=AuthInput,
    ),
    'ApiKey': define_factory(
        create=_create_api_key,
        teardown=lambda record, ctx: Users.delete_user_api_key_by_id(record['user_id']),
        input_model=ApiKeyInput,
    ),
    'Group': define_factory(
        create=_create_group,
        teardown=lambda record, ctx: Groups.delete_group_by_id(record['id']),
        input_model=GroupInput,
    ),
    'GroupMember': define_factory(
        create=_create_group_member,
        teardown=lambda record, ctx: Groups.remove_users_from_group(record['group_id'], [record['user_id']]),
        input_model=GroupMemberInput,
    ),
    'AccessGrant': define_factory(
        create=_create_access_grant,
        teardown=_teardown_access_grant,
        input_model=AccessGrantInput,
    ),
    'Model': define_factory(
        create=_create_model,
        teardown=lambda record, ctx: Models.delete_model_by_id(record['id']),
        input_model=ModelInput,
    ),
    'Folder': define_factory(
        create=_create_folder,
        teardown=_teardown_folder,
        input_model=FolderInput,
    ),
    'Chat': define_factory(
        create=_create_chat,
        teardown=_teardown_chat,
        input_model=ChatInput,
    ),
    'ChatMessage': define_factory(
        create=_create_chat_message,
        teardown=_teardown_chat_message,
        input_model=ChatMessageInput,
    ),
    'ChatFile': define_factory(
        create=_create_chat_file,
        teardown=_teardown_chat_file,
        input_model=ChatFileInput,
    ),
    'Tag': define_factory(
        create=_create_tag,
        teardown=_teardown_tag,
        input_model=TagInput,
    ),
    'Feedback': define_factory(
        create=_create_feedback,
        teardown=lambda record, ctx: Feedbacks.delete_feedback_by_id(record['id']),
        input_model=FeedbackInput,
    ),
    'SharedChat': define_factory(
        create=_create_shared_chat,
        teardown=_teardown_shared_chat,
        input_model=SharedChatInput,
    ),
    'File': define_factory(
        create=_create_file,
        teardown=lambda record, ctx: Files.delete_file_by_id(record['id']),
        input_model=FileInput,
    ),
    'Knowledge': define_factory(
        create=_create_knowledge,
        teardown=_teardown_knowledge,
        input_model=KnowledgeInput,
    ),
    'KnowledgeDirectory': define_factory(
        create=_create_knowledge_directory,
        teardown=_teardown_knowledge_directory,
        input_model=KnowledgeDirectoryInput,
    ),
    'KnowledgeFile': define_factory(
        create=_create_knowledge_file,
        teardown=_teardown_knowledge_file,
        input_model=KnowledgeFileInput,
    ),
    'Channel': define_factory(
        create=_create_channel,
        teardown=_teardown_channel,
        input_model=ChannelInput,
    ),
    'ChannelMember': define_factory(
        create=_create_channel_member,
        teardown=_teardown_channel_member,
        input_model=ChannelMemberInput,
    ),
    'ChannelFile': define_factory(
        create=_create_channel_file,
        teardown=_teardown_channel_file,
        input_model=ChannelFileInput,
    ),
    'ChannelWebhook': define_factory(
        create=_create_channel_webhook,
        teardown=_teardown_channel_webhook,
        input_model=ChannelWebhookInput,
    ),
    'Message': define_factory(
        create=_create_message,
        teardown=lambda record, ctx: Messages.delete_message_by_id(record['id']),
        input_model=MessageInput,
    ),
    'MessageReaction': define_factory(
        create=_create_message_reaction,
        teardown=_teardown_message_reaction,
        input_model=MessageReactionInput,
    ),
    'Automation': define_factory(
        create=_create_automation,
        teardown=_teardown_automation,
        input_model=AutomationInput,
    ),
    'AutomationRun': define_factory(
        create=_create_automation_run,
        teardown=_teardown_automation_run,
        input_model=AutomationRunInput,
    ),
    'Calendar': define_factory(
        create=_create_calendar,
        teardown=_teardown_calendar,
        input_model=CalendarInput,
    ),
    'CalendarEvent': define_factory(
        create=_create_calendar_event,
        teardown=lambda record, ctx: CalendarEvents.delete_event_by_id(record['id']),
        input_model=CalendarEventInput,
    ),
    'CalendarEventAttendee': define_factory(
        create=_create_calendar_event_attendee,
        teardown=_teardown_calendar_event_attendee,
        input_model=CalendarEventAttendeeInput,
    ),
    'Prompt': define_factory(
        create=_create_prompt,
        teardown=_teardown_prompt,
        input_model=PromptInput,
    ),
    'PromptHistory': define_factory(
        create=_create_prompt_history,
        teardown=_teardown_prompt_history,
        input_model=PromptHistoryInput,
    ),
    'Note': define_factory(
        create=_create_note,
        teardown=_teardown_note,
        input_model=NoteInput,
    ),
    'PinnedNote': define_factory(
        create=_create_pinned_note,
        teardown=_teardown_pinned_note,
        input_model=PinnedNoteInput,
    ),
    'Tool': define_factory(
        create=_create_tool,
        teardown=_teardown_tool,
        input_model=ToolInput,
    ),
    'Skill': define_factory(
        create=_create_skill,
        teardown=_teardown_skill,
        input_model=SkillInput,
    ),
    'Function': define_factory(
        create=_create_function,
        teardown=lambda record, ctx: Functions.delete_function_by_id(record['id']),
        input_model=FunctionInput,
    ),
    'Memory': define_factory(
        create=_create_memory,
        teardown=lambda record, ctx: Memories.delete_memory_by_id(record['id']),
        input_model=MemoryInput,
    ),
    'OAuthSession': define_factory(
        create=_create_oauth_session,
        teardown=_teardown_oauth_session,
        input_model=OAuthSessionInput,
    ),
    'Config': define_factory(
        create=_create_config,
        teardown=_teardown_config,
        input_model=ConfigInput,
    ),
}
