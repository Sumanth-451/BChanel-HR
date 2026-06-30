"""
Parse Zoho Recruit CSV export into candidate dicts.
Expected columns (flexible — maps by header name):
  Record Id, Application Name, First Name, Last Name,
  Application Stage, Posting Title, Email, Mobile, Skill Set
"""
import csv
import io
from typing import Any


_COLUMN_MAP = {
    "record id":        "record_id",
    "application name": "name",
    "first name":       "first_name",
    "last name":        "last_name",
    "application stage":"stage",
    "application id":   "application_id",
    "posting title":    "target_role",
    "email":            "email",
    "mobile":           "mobile",
    "skill set":        "skills",
}


def parse_zoho_csv(content: bytes) -> list[dict[str, Any]]:
    """
    Parse raw CSV bytes from a Zoho Recruit export.
    Returns list of candidate dicts with normalised keys.
    """
    text = content.decode("utf-8-sig").strip()
    reader = csv.DictReader(io.StringIO(text))

    candidates = []
    for row in reader:
        candidate: dict[str, Any] = {}
        for raw_key, value in row.items():
            norm = raw_key.strip().lower()
            mapped = _COLUMN_MAP.get(norm, norm.replace(" ", "_"))
            candidate[mapped] = (value or "").strip()

        # Build display name
        if not candidate.get("name"):
            first = candidate.get("first_name", "")
            last  = candidate.get("last_name", "")
            candidate["name"] = f"{first} {last}".strip()

        candidates.append(candidate)

    return candidates
