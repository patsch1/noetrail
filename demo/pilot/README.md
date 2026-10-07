# Synthetic alpha-pilot kit

Use this kit with the [pilot plan](../../docs/alpha-pilot.md). It contains two
fictional Obsidian notes, a declarative saved view, a generated 16×16 checkerboard
PNG with no photo metadata, and an anonymous scorecard. There are no participant
results in this directory.

## Prepare a disposable instance

Install the published alpha in a virtual environment as described in
[Installation](../../docs/installation.md). Run the following from a source
checkout to copy the fixtures; none of these commands writes into the checkout:

```sh
PILOT_ROOT="$(mktemp -d)"
printf '%s\n' "$PILOT_ROOT"
cp -R demo/data "$PILOT_ROOT/data"
cp -R demo/config "$PILOT_ROOT/config"
cp demo/pilot/views.yaml "$PILOT_ROOT/config/views.yaml"
mkdir -p "$PILOT_ROOT/data/imports/raw/pilot" "$PILOT_ROOT/inbox"
cp demo/pilot/obsidian/*.md "$PILOT_ROOT/data/imports/raw/pilot/"
cp demo/pilot/synthetic-checker.png "$PILOT_ROOT/inbox/"
.venv/bin/noetrail --data-root "$PILOT_ROOT/data" \
  --config-root "$PILOT_ROOT/config" validate
```

For the participant's client, launch `.venv/bin/noetrail-mcp` using its absolute
path and these arguments:

```text
--data-root <PILOT_ROOT>/data --config-root <PILOT_ROOT>/config
--attachment-inbox <PILOT_ROOT>/inbox
```

Replace `<PILOT_ROOT>` with the printed temporary path; the angle-bracket text
is a placeholder. Alternatively use the pinned `uvx` launcher in
[MCP clients](../../docs/integrations/mcp-clients.md). Keep this instance separate
from all personal vaults.

## Participant tasks

The facilitator records observations but does not coach the first attempt.
Ask the participant to:

1. Install the alpha and connect the client. Retrieve the stored Azure Harbor
   destination. Record time to the first successful retrieval and any setup help.
2. Capture a fictional note titled “Pilot observation”. Attach
   `synthetic-checker.png` from the fixed inbox and retrieve the image.
3. Preview `import obsidian --source pilot`, apply it, and repeat it. Inspect
   the skipped duplicates and the unresolved “Missing winter plan” reference.
   Imports are local CLI operations; the vault MCP server has no import tool.
4. Ask: “What does Cedar bicycle maintenance say?”, “Was soll ich nach einer
   Regenfahrt mit der Kette machen?”, “What should happen once the chain is
   dry?”, and “What tire pressure is specified?”. The first three have grounded
   sample answers; the last fact is absent. A missed relevant entry is a
   retrieval failure, not evidence that the sample contains no answer.
5. Add a synthetic dated `related_to` relationship between the two bicycle
   notes using the documented relation workflow. Ask for the relationship
   before and during its validity interval. Record the explicit fixture dates
   and expected presence/absence in the scorecard.
6. Correct the observation note, try a stale revision deliberately, then inspect
   a duplicate merge preview before applying it. Verify retained source entries
   in trash. Merge is a separate decision; do not apply an unclear preview.
7. Run `pilot_destinations` and verify Azure Harbor. Stop client writers, copy
   both data and configuration roots into a paired backup, and restore into
   new empty roots. Validate and compare the selected entry IDs, revisions,
   relation, travel pack, view result, and attachment SHA-256. Reconnect the
   client to the restored roots to prove it does not depend on the originals.

See [saved views](../../docs/saved-views.md),
[relation validity](../../docs/architecture.md#relations-carry-valid-time-the-envelope-carries-record-time), and
[backup/restore](../../docs/backup-restore.md) for the exact interfaces.

## Expected fixture facts

- Azure Harbor is a fictional `travel/destination` in Exampleland.
- The bicycle note says to wipe the chain dry after rain, then oil it once dry.
- The supplied service log has a fictional check date of 2026-01-15.
- No tire-pressure value or battery capacity is supplied.
- The unresolved winter-plan link remains visible after import.
- Repeating an unchanged import creates no duplicate entries.
- The image is a generated checkerboard, not a photograph.

Copy [scorecard.md](scorecard.md) outside the source checkout. Use anonymous
labels such as P01. Retain observations only with consent, and never commit
participant data, full client logs, personal paths or screenshots to this kit.
Remove the disposable roots only after the session and its restore checks are
finished; they are separate from any real vault.
