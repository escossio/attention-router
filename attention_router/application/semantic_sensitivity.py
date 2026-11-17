"""Translation between archive and V2 sensitivity; never disclosure authority."""
from dataclasses import dataclass


@dataclass(frozen=True)
class SensitivityTranslation:
    original: str
    candidate_class: str
    restricted: bool
    mapping_version: str = 'ARCHIVE_TO_V2_V1'


def translate_sensitivity(value) -> SensitivityTranslation:
    original = value.strip().upper() if isinstance(value, str) else 'UNKNOWN'
    mapped = {'NORMAL': 'NORMAL', 'PERSONAL': 'PRIVATE', 'SENSITIVE': 'PRIVATE',
              'PRIVATE': 'PRIVATE', 'SECRET': 'SECRET'}.get(original, 'SECRET')
    return SensitivityTranslation(original, mapped, mapped != 'NORMAL')
