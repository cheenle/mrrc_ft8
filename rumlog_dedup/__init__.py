"""RUMLogNG duplicate cleanup tool (read-only unless --apply).

A deliberate, backup-protected, operator-confirmed exception to AD-016's
"RUMLogNG is read-only" rule.  The sync program (rumlog_sync) stays read-only;
this tool only writes ZCORE_QSO when the operator passes --apply with a
verified backup.  It is never scheduled and never imported by the sync.
"""

__version__ = "0.1.0"
