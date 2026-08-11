# Autonoma Environment Factory — implementation checklist

SDK endpoint path: `/api/autonoma` (the default — no override needed).

Stack discovered: FastAPI + async SQLAlchemy backend (`backend/open_webui`), SvelteKit
frontend (`src/`), SQLite at `backend/data/webui.db` by default. SDK installed:
`autonoma-ai[fastapi]==0.2.9`.

## Endpoint, auth, teardown

- [x] `autonoma-ai[fastapi]` added to `backend/requirements.txt` and `pyproject.toml`
- [x] Endpoint mounted at `/api/autonoma` (`backend/open_webui/autonoma/__init__.py`,
      wired in `main.py`), signature verified by the SDK against
      `AUTONOMA_SHARED_SECRET` from the environment
- [x] `scope_field = "user_id"` — Open WebUI has no tenant/org table; every
      user-owned row is scoped by `user_id`
- [x] Auth callback returns real credentials: a `token` cookie (what the frontend
      reads) + `Authorization: Bearer` header, both minted by the app's own
      `create_token`, plus the seeded email/password for login-screen tests
- [x] Teardown implemented per entity, in reverse dependency order (see note below)
- [x] Maintenance note added (no agent-instructions file existed, so `AGENTS.md`
      was created at the repo root)
- [x] Wrong signature is rejected (verified — see Validation)
- [x] Full-recipe `up` / `down` pass
- [x] `sdk check` on `recipe.json` prints `"ok": true`
- [x] Concurrent-instances proof (`--repeat 3`) passes
- [x] Branch pushed + pull request opened
      (https://github.com/Autonoma-Labs/open-webui/pull/1, base `main`)

### How teardown is scoped

Open WebUI has no tenant table to delete, so there is no single root that removes
everything. `Auths.delete_auth_by_id` cascades some of it (the user row, the
credential, all their chats, group memberships), but most user-owned tables —
notes, knowledge, files, channels, calendars, automations, prompts, tools,
skills, functions, memories — are not reached by it, and SQLite runs here without
enforced foreign keys. Teardown is therefore per entity: each factory calls the
app's own delete function, and the SDK runs them in reverse `_alias`/`_ref` order
so children go before their parents.

Where the app *does* have a scoping root, teardown deletes by it, because that
also removes rows a test created which were never part of `up` — the case
per-record teardown leaks:

* **`Channel`** — `delete_channel_by_id` removes the channel row and its access
  grants but leaves memberships, files, webhooks and messages behind. The
  teardown clears the whole subtree. This is not just tidiness: posting a message
  joins its author to the channel as a side effect, so a channel accumulates
  membership rows nothing in `up` ever recorded. Before this was fixed, a
  full cycle left orphaned `channel_member` rows behind; it now leaves none.
* **`Knowledge`** — `reset_knowledge_by_id(include_directories=True)` first, so
  files and directories go too, including anything uploaded into the base
  mid-test.
* **`Chat`** — `delete_chat_by_id` already cascades `chat_message` and the share
  snapshot, but not `chat_file`, so attachments are cleared first.
* **`Calendar`** — `delete_calendar_by_id` already cascades its events and
  attendees, so no extra work is needed.

Deletes are scoped strictly to ids the seed created; nothing is removed by
pattern or by table sweep.

## Factories — every entity from `entity-audit.md`

Validated = `sdk up` on a slice, rows confirmed in SQLite, `sdk down`, rows
confirmed gone.

| # | Entity | Real creation function called | Validated |
|---|--------|-------------------------------|-----------|
| 1 | User | `Auths.insert_new_auth` (writes `auth` + `user`, hashes password) | [x] |
| 2 | Auth | `Auths.insert_new_auth` | [x] |
| 3 | ApiKey | `Users.update_user_api_key_by_id` | [x] |
| 4 | Group | `Groups.insert_new_group` | [x] |
| 5 | GroupMember | `Groups.add_users_to_group` | [x] |
| 6 | AccessGrant | `AccessGrants.grant_access` | [x] |
| 7 | Model | `Models.insert_new_model` | [x] |
| 8 | Folder | `Folders.insert_new_folder` | [x] |
| 9 | Chat | `Chats.insert_new_chat` (+ archive/pin toggles) | [x] |
| 10 | ChatMessage | `ChatMessages.upsert_message` | [x] |
| 11 | ChatFile | `Chats.insert_chat_files` | [x] |
| 12 | Tag | `Tags.insert_new_tag` + `Chats.add_chat_tag_by_id_and_user_id_and_tag_name` | [x] |
| 13 | Feedback | `Feedbacks.insert_new_feedback` | [x] |
| 14 | SharedChat | `SharedChats.create` + `Chats.update_chat_share_id_by_id` | [x] |
| 15 | File | `Files.insert_new_file` | [x] |
| 16 | Knowledge | `Knowledges.insert_new_knowledge` | [x] |
| 17 | KnowledgeDirectory | `Knowledges.create_directory` | [x] |
| 18 | KnowledgeFile | `Knowledges.add_file_to_knowledge_by_id` | [x] |
| 19 | Channel | `Channels.insert_new_channel` | [x] |
| 20 | ChannelMember | `Channels.join_channel` | [x] |
| 21 | ChannelFile | `Channels.add_file_to_channel_by_id` | [x] |
| 22 | ChannelWebhook | `Channels.insert_webhook` | [x] |
| 23 | Message | `Messages.insert_new_message` | [x] |
| 24 | MessageReaction | `Messages.add_reaction_to_message` | [x] |
| 25 | Automation | `Automations.insert` (+ `next_run_ns` from the rrule) | [x] |
| 26 | AutomationRun | `AutomationRuns.insert` | [x] |
| 27 | Calendar | `Calendars.insert_new_calendar` | [x] |
| 28 | CalendarEvent | `CalendarEvents.insert_new_event` | [x] |
| 29 | CalendarEventAttendee | `CalendarEventAttendees.set_attendees` | [x] |
| 30 | Prompt | `Prompts.insert_new_prompt` | [x] |
| 31 | PromptHistory | `PromptHistories.create_history_entry` | [x] |
| 32 | Note | `Notes.insert_new_note` | [x] |
| 33 | PinnedNote | `Notes.toggle_note_pinned_by_id` | [x] |
| 34 | Tool | `Tools.insert_new_tool` (specs from `get_tool_specs`) | [x] |
| 35 | Skill | `Skills.insert_new_skill` | [x] |
| 36 | Function | `Functions.insert_new_function` | [x] |
| 37 | Memory | `Memories.insert_new_memory` | [x] |
| 38 | OAuthSession | `OAuthSessions.create_session` | [x] |
| 39 | Config | `Config.upsert` | [x] |

No entity fell back to a raw insert: every factory calls the app's own
data-access function. Three notes on faithfulness:

* **`ChatFile`** — the audit named `ChatsTable.add_files_to_chat_message`, which
  does not exist. The real path is `ChatsTable.insert_chat_files`, used instead.
  It keeps the app's own check that the caller may read the file before linking.
* **`Memory`** — the memories *router* also writes an embedding to the vector
  store. That is a request-scoped external-service side effect and is
  deliberately not reproduced; every memory screen reads the relational row.
* **`Tool`** — specs are derived with the app's `load_tool_module_by_id` +
  `get_tool_specs`, exactly as `POST /tools/create` does. If a module fails to
  load the factory falls back to empty specs rather than failing the seed; the
  seeded tool content is valid and does produce real specs.

### `Auth` is not in the standard recipe

In Open WebUI a local credential cannot exist without its user — `insert_new_auth`
writes both rows in one transaction. The `User` factory therefore already mints
the `auth` row, so seeding `Auth` again in the same recipe would create a second
account. Per the SDK's guidance on transitively-minted models, `Auth` is
registered (so the model is addressable and discoverable) but left out of the
`create` graph. It was validated independently with its own slice.

## Fields compared against the current time

These are the columns the application branches on being before/after now. Each is
supplied to its factory as an **offset** and resolved against the clock at seeding
time, so the recipe stays correct however long it is replayed.

| Column | Where the app branches on it | Recipe input |
|--------|------------------------------|--------------|
| `chat.updated_at` / `created_at` | Sidebar buckets chats into Today / Yesterday / Previous 7 days (`src/lib/utils/index.ts`) | `updated_minutes_ago`, `created_minutes_ago` |
| `calendar_event.start_at` / `end_at` | `get_events_by_range` window + upcoming-event reminders (`start_at >= now_ns`) | `starts_in_minutes`, `duration_minutes`, `anchor_hour` |
| `automation.next_run_at` | Scheduler claims rows with `next_run_at <= now_ns` and runs them | derived from the rrule by the app's own `next_run_ns()` — always in the future |
| `oauth_session.expires_at` | Session expiry | `expires_in_minutes` (default 720) |
| `automation_run.created_at` | `get_latest` picks max `created_at` as "last run" | `created_minutes_ago` (orders the two runs) |
| `message.created_at` | Channel thread ordering | `created_minutes_ago` |

Units differ per table and are handled inside the factories: `chat`, `file`,
`note`, `memory`, `oauth_session` store epoch **seconds**; `message`,
`message_reaction`, `calendar_event`, `automation`, `automation_run` store epoch
**nanoseconds**.

## Uniqueness rules and per-run tokens

Enumerated from the model definitions, the migrations, and the live SQLite
schema. Every rule below is covered by a `{{testRunId}}` / `{{testRunShortId}}`
token in the recipe, or by an id the app itself mints per run.

| Constraint | How it is made per-run |
|------------|------------------------|
| `user.email` UNIQUE (+ `ix_user_email_normalized` unique index) | `{{testRunId}}` in every email |
| `api_key.key` UNIQUE | `sk-{{testRunShortId}}-…` |
| `prompt.command` UNIQUE (global, not per user) | `/code-{{testRunShortId}}` |
| `skill.name` UNIQUE (global, not per user) | name carries `{{testRunShortId}}` |
| `model.id`, `tool.id`, `skill.id`, `function.id`, `file.id` (caller-supplied PKs) | each carries `{{testRunShortId}}` |
| `access_grant` UNIQUE (resource_type, resource_id, principal_type, principal_id, permission) | `resource_id` / `principal_id` are per-run ids resolved by `_ref` |
| `tag` PK (id, user_id) | `user_id` is a fresh UUID per run |
| `knowledge_directory` UNIQUE (knowledge_id, parent_id, name) | `knowledge_id` is per-run |
| `knowledge_file` UNIQUE (knowledge_id, file_id) | both per-run |
| `chat_file` UNIQUE (chat_id, file_id) | both per-run |
| `channel_file` UNIQUE (channel_id, file_id) | both per-run |
| `calendar_event_attendee` UNIQUE (event_id, user_id) | both per-run |
| `chat.share_id` UNIQUE | minted by `SharedChats.create` |
| all other PKs | UUIDs minted by the app's creation functions |

### Documented limitation: the `config` table is a global singleton

`config` is keyed by the config key itself (`PRIMARY KEY (key)`), and the
application reads a fixed key — `ui.name` — to render the site name. There is no
per-run variant of that row: two runs seeding it necessarily address the same
primary key. It cannot be made per-run without changing the app's schema.

`Config.upsert` means this does not *fail* under concurrency (the third `up`
overwrites rather than erroring, and `--repeat 3` passes), but concurrent runs
share one value. To stay non-destructive the factory captures the previous value
on `up` and restores it on `down` — deleting the key outright would discard a
real setting — so a run never leaves the instance's configuration changed.

## Validation

Driven through the planner CLI's signed client against
`http://127.0.0.1:8080/api/autonoma`, with every assertion confirmed by querying
`backend/data/webui.db` directly.

**`discover`** — 39 models, `scopeField: "user_id"`.

**Per entity (all 39)** — each was sliced out with the transitive closure of its
`_ref` parents, seeded with `sdk up`, every created row located in SQLite by
primary key, torn down with `sdk down`, and confirmed gone. 39/39 pass, and the
39 consecutive cycles left all 38 tables empty.

**Full recipe** — 72 rows across 38 entities, `up` 912 ms / `down` 290 ms. After
`down`, all 38 tables are empty: not only the 72 seeded rows but also the
side-effect rows never listed in `up` (the channel memberships that posting a
message creates), which is what the scoping-root teardowns above exist for.

**Signature enforcement** — a wrong signature, a missing signature, and a valid
signature over a *different* body are all rejected with
`401 INVALID_SIGNATURE`; only the correctly signed request returns 200.

**Auth payload** — verified against the running app, not just inspected:
- `Authorization: Bearer <jwt>` → `GET /api/v1/auths/` returns 200 with the
  seeded admin's identity
- the `token` cookie → same endpoint returns 200
- the returned email/password → `POST /api/v1/auths/signin` returns 200
- no credentials → 401

The two seeded API keys exist with the required `sk-` prefix, but API-key auth
returns 403 on this instance because `auth.enable_api_keys` is `false` in its
config — an instance setting, not a defect in the factory.

**Time-sensitive rows** — checked by running the application's own queries, so
what is asserted is what the product shows:
- `Chats.get_chat_list_by_user_id` → "Learning Python" and "Daily Standup Prep"
  fall in Today, "Public AI Tips" in Previous 7 days, "Old Project Planning"
  older still
- `CalendarEvents.get_events_by_range` → "Weekly Sync" is upcoming, "Release
  Party" is past, and both are visible in the calendar's range window
- both automations have `next_run_at` in the future, so the scheduler (which
  claims `next_run_at <= now`) does not fire them mid-test
- `AutomationRuns.get_latest` → the success run for one automation, the error run
  for the other, i.e. the intended ordering
- the seeded OAuth session has not drifted past its expiry

**Real side effects confirmed in the DB** — tool `specs` are the genuine output of
the app's `get_tool_specs` (real JSON-schema parameters, not empty); each prompt
has its automatic "Initial version" history entry with `version_id` pinned, plus
the seeded revision; `chat_message` holds 11 rows, 9 of them dual-written by
`insert_new_chat`.

**Concurrency (`--repeat 3`)** — all three instances came up 200 while live
simultaneously, all three tore down, and the database was empty afterwards. The
CLI reports: *"All 3 instances were live at the same time, so nothing in this
recipe is single-instance."*

**`sdk check`** — `"ok": true`, no problems, 38 entities / 72 records.
