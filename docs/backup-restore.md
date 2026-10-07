# Backup and restore

## Goal

Your backup job protects the personal data volume independently of Git. A
backup only counts as trustworthy once a restore onto a new, empty volume has
been tested successfully.

## What to back up

At a minimum, back up:

- `vault/`, including `vault/attachments/`;
- `trash/`, for as long as reversible deletions should stay recoverable;
- raw imports, if they cannot be regenerated from the source system.

Installed code, bundled schemas, skills, and documentation can be restored
from the matching release. **Instance configuration cannot be reconstructed
from that release.** Back up the config root separately with restricted access:
`packs/` (including templates), `views.yaml`, `config.yaml` and
`relation-types.yaml`. Record the release version and pair this configuration
with the same data backup generation. Custom entries need their matching packs
to validate; views and relation definitions are part of the usable instance.

The derived `index/` directory may be excluded and rebuilt. Keep credentials
in their separately managed secret backup, outside both archives. In the
legacy combined layout, instance configuration lives under `.knowledge/`.
Take a consistent filesystem snapshot or stop writers while copying data and
configuration; an ordinary live recursive copy is not a transaction.

`vault/.locks/` contains no knowledge data and may be excluded. A restored lock
file is harmless either way, because the actual process lock is not stored in
the file contents.

## Requirements for the backup job

The concrete deployment configuration should document at least the following,
outside this repository:

| Property | Value to document |
| --- | --- |
| Schedule | actual execution frequency |
| RPO | maximum acceptable data loss |
| RTO | target time until restore is complete |
| Retention | daily, weekly, and long-term generations |
| Encryption | method and key ownership |
| Destination | a system or account separate from the live volume |
| Monitoring | alert on failure or an overdue backup |
| Integrity check | checksum or the tool's native verify procedure |

Backup credentials must live neither in the repository nor in an agent
workspace.

## Regular restore test

Run this after every change to the backup job, and regularly afterwards:

1. Pick one specific backup generation and record its identifier.
2. Create a new, empty test volume. Never restore onto the live volume first.
3. Restore the generation into `vault/` and `trash/` on the test volume.
4. Install the matching release and restore the paired instance configuration
   onto a separate empty config directory. Include custom packs and views.
5. Validate:

   ```sh
   noetrail --data-root /srv/noetrail-restore/data \
     --config-root /srv/noetrail-restore/config validate
   ```

6. Compare the number and size of restored files against the backup inventory.
7. Open samples across several types: thought, memory, bookmark, and at least
   one attachment.
8. Run a review, a relations read, and a saved view against the restored roots.
   Include a custom-type entry and compare IDs, revisions and attachment bytes.
   The original data and config roots must not be available during the test.
9. Confirm that the secret store and other unrelated volumes are not part of
   the restore.
10. Record the outcome, duration, backup identifier, and any deviations, then
    remove the test volume.

`tests/test_backup_restore.py` exercises this with synthetic custom entries,
a local schema pack, a saved view, a relation and an attachment, removing the
original roots before validation. This checks the application contract; the
regular exercise above must still verify your actual backup system.

## Recovering from an incident

1. Stop all writes to the damaged live vault.
2. Determine the root cause and the last known-good backup generation.
3. Restore onto a new volume and run `validate` there.
4. Check samples and attachments.
5. Only then point the knowledge agent at the new volume.
6. Keep the old volume unchanged until the root-cause analysis is finished.

## Trash and backup retention

`trash/` has a local retention of 90 days by default. Backup generations have
their own retention, so a local `purge` does not remove existing backup copies.
When permanent deletion is required, both lifecycles have to be considered and
documented.
