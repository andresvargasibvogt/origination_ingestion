"""Secondary document acquisition — annexes linked from BOE announcements.

BOE Section V announcements (BOE-B-…) are pointers; the substantive technical
project, environmental study, and cadastral survey live in annex files behind
the `almacen.redsara.es` public file-exchange portal. This package discovers
those links in each announcement's XML, fetches the files (a plain public HTTP
API — no headless browser), and lands them in OneLake bronze so the downstream
extraction pass can read them.

The portal links expire ~3 months after creation, so acquisition is reliable
going forward (run right after announcement ingestion) and best-effort for the
recent past.
"""
