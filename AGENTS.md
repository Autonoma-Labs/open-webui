# Agent instructions

## Autonoma test data

Autonoma is an end-to-end testing service: it drives the real UI against a preview
deployment of this repo. Before each run it seeds an isolated set of test data by
calling one signed endpoint in this backend — `/api/autonoma`
(`backend/open_webui/autonoma/`) — and calls it again afterwards to remove
everything it created. The seeding happens through **factories that call this
app's own creation functions** (`Auths.insert_new_auth`, `Chats.insert_new_chat`,
`Knowledges.insert_new_knowledge`, …), so seeded rows get the same password
hashing, access-grant wiring, version snapshots and message dual-writes that
rows created through the UI do.

**When you add or change a model — or change the code that creates one — update
the matching factory in `backend/open_webui/autonoma/factories.py`.** A new model
needs a new entry in `FACTORIES` (an input model, a `create` that calls the real
creation function, and a `teardown`); a changed creation function or a new
required field needs the existing factory adjusted to match. A factory that has
drifted from the schema fails every test run at the seeding step, before a single
assertion executes.

Two things to preserve when editing factories:

- **Any column the app compares against the current time takes an offset**
  (`*_minutes_ago` / `*_in_minutes`) that the factory resolves against the clock
  at seeding time. The recipe is stored once and replayed for months, so a fixed
  timestamp goes stale and the suite fails as if the product were broken.
- **Values in unique columns must be per-run.** Tests run concurrently; either
  put the per-run token in the recipe or derive the value from the `test_run_id`
  on the factory context.

`IMPLEMENTATION.md` records which creation function each factory calls, how
teardown is scoped, and the known limitation around the global `config` table.
