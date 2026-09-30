# Proximus Client Continuity Mock Data

Synthetic mock source pack for the SD Worx Tectonic Hackathon proof of concept.

This round uses more realistic enterprise file formats for SharePoint and OneDrive:

- Word documents for narrative manuals and handover notes.
- Excel workbooks for payroll calendars and allowance matrices.
- PDF for a stale payment procedure.
- Single-file JSON exports for Outlook and Microsoft Teams.

## Layout

- `manifest.json`: counts, formats, and source notes.
- `client-memory/`: consultant-facing client memory in JSON and Word format.
- `sharepoint-onedrive/document-index.json`: metadata for the five source documents.
- `sharepoint-onedrive/documents/`: five realistic Office/PDF documents.
- `outlook/outlook_export.json`: 15 email messages in one export file.
- `teams/teams_export.json`: 100 Teams messages in one export file. Direct messages are excluded.
- `derived/`: seeded conflicts and demo continuity questions.

## Client memory coverage

The client memory explicitly includes standing rules, exceptions, payroll deadlines, decisions, open issues, conflicting information, recent changes, and people/owners.

## Teams format

Teams messages include timestamps in `sent_at`. The export includes channel posts, channel replies, group chat messages, and meeting chat messages. Attachments can reference SharePoint or OneDrive documents with `sharepoint_document_id`.
