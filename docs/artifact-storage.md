# Artifact Storage Decision

Arranger should keep structured application state in Postgres and store
generated files in object storage once export features exist.

## Keep In Postgres

- users
- sessions
- profiles
- scores as JSON
- arrangement plans as JSON
- verifier results
- run metadata
- artifact metadata

These records are small, queryable, and tied to permissions.

## Move To Object Storage

- generated MIDI files
- generated MusicXML files
- generated PDF scores
- source audio uploads
- large transcription intermediates

These artifacts can become large, are usually downloaded as whole files, and do
not need relational queries over their full contents.

## Recommended Provider

Use Cloudflare R2 or AWS S3 behind an `ArtifactStore` port when exports are
implemented. R2 is a good first choice for this project because it avoids egress
fees for common download-heavy workflows and uses the S3 API.

## Future Shape

Add an `artifacts` table:

```text
id
user_id
arrangement_id
kind
content_type
storage_key
size_bytes
created_at
```

The API should authorize access through Postgres, then issue signed URLs or
stream files through the backend. Do not put raw object storage URLs directly in
public records.
