# Encrypted backup and staged restore

Android pending job IDs live in each phone's private app preferences. Spark remains authoritative for archive, run, job, result, and audit state.

Followthrough backups preserve the three durable ledgers and the opaque runtime artifacts needed
for recovery:

- `data/followthrough.db` (operations);
- `data/archive/archive.db` (ciphertext-only transcript archive);
- `data/effects/effects.db` (typed effects and receipts);
- encrypted `.fta` audio chunks; and
- runner receipt files.

The backup path is deliberately allowlisted. Device tokens, the archive key, OAuth credentials,
`.env` files, Hermes state, decrypted transcripts, and runner workspaces are not read or copied.
Audio and receipts are copied byte-for-byte without parsing or decryption.

## Create and verify

Choose a new destination that does not exist. Creation happens in a private sibling staging
directory and becomes visible through one atomic rename only after all database snapshots,
hashes, permissions, and SQLite integrity checks succeed.

```bash
.venv/bin/python scripts/followthrough-backup.py create \
  --destination data/backups/followthrough-$(date -u +%Y%m%dT%H%M%SZ)

.venv/bin/python scripts/followthrough-backup.py verify \
  data/backups/followthrough-20260711T220000Z
```

Every directory in the artifact is `0700`; every file is `0600`. `manifest.json` records the
SHA-256 digest, byte size, mode, category, and relative path of each payload. A separate
`manifest.sha256` detects manifest modification. Verification also rejects symlinks, special
files, missing or extra paths, unsafe modes, and corrupt SQLite snapshots.

This is an integrity manifest, not a digital signature. Store the backup on encrypted storage and
protect the transport separately.

## Restore rehearsal

Restore never writes to configured live paths. It only accepts a target argument naming a real,
already-existing, completely empty directory. The backup is fully verified before copying, the
staged result is verified again, and the empty target is atomically replaced.

```bash
mkdir -m 700 /tmp/followthrough-restore-rehearsal
.venv/bin/python scripts/followthrough-backup.py restore \
  data/backups/followthrough-20260711T220000Z \
  --target /tmp/followthrough-restore-rehearsal
```

The restored tree is a recovery bundle, not an automatic live-data replacement. An operator must
stop services, inspect the verified rehearsal, map its three database files and opaque artifact
trees to a separate recovery configuration, and only then perform a separately approved cutover.
The restore command itself cannot overwrite a nonempty directory.

## Verification boundary

The backup layer proves a consistent copy and byte-level restore. It intentionally never decrypts
an archive item, so a complete disaster-recovery drill should separately test decryption with the
owner-held archive key in an isolated recovery environment. That key must be recovered from its
independent secret backup; it is never part of this bundle.
