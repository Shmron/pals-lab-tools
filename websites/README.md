# Websites

Static source for the two public sites of the lab.

| Folder | Site | What it is |
|---|---|---|
| [`placealert-org`](placealert-org) | <https://placealert.org> | The lab's public website: about, team, research, publications, partners, courses and news |
| [`portal-placealert-org`](portal-placealert-org) | <https://portal.placealert.org> | The PALs Portal, the front door to the lab's tools, data access and courses |

## Notes

- Pages for tools developed by partners (for example SPECTRA, GIPEX and the CHEAQI data catalogue) are **not** included in this repository.
- Team and contact details on these pages are already public on the live sites. Everything else (data folders, backups, uploads) was left out.
- The portal links to the tools in [`../tools`](../tools). Some portal folders are symlinks on the server to generated map data, which is not included here.
- Serve either folder with any static file server, for example `python -m http.server` inside the folder.
