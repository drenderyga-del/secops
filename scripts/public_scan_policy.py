"""Shared deterministic path-credential rules for public publication gates."""

# Low-entropy token filenames must also fail the path-manifest scan.
PATH_SCANNER_RULE = br"""
[[rules]]
id = "public-filename-credential"
description = "Credential-shaped filename"
regex = '''(?i)(?:glpat|glrt|glcbt|gldt|glsoat|gloas|glagent|glft|glptt)[-_][a-z0-9_-]{12,}|github_pat_[a-z0-9_]{20,}|gh[pousr]_[a-z0-9]{20,}|xox[baprs]-[a-z0-9-]{12,}|(?:private[_-]?token|access[_-]?token|api[_-]?key|password|secret)[=:][^/\s]{4,}'''
"""

def has_path_collisions(paths) -> bool:
    """Reject Git names that alias files/directories on common macOS volumes."""
    import unicodedata
    from pathlib import PurePosixPath

    seen = {}
    for path in paths:
        parts = PurePosixPath(path).parts
        for index in range(1, len(parts) + 1):
            prefix = '/'.join(parts[:index])
            key = unicodedata.normalize('NFC', unicodedata.normalize('NFC', prefix).casefold())
            identity = (prefix, index == len(parts))
            if key in seen and seen[key] != identity:
                return True
            seen[key] = identity
    return False
