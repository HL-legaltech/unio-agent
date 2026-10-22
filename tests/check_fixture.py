"""Checks tests/fixtures/skill_block.json, the get_me skill block that the
backend's spec copies verbatim. The plugin's shell scripts read that block
with sed, so it must stay flat: no nested objects or arrays.

Usage: python3 -I tests/check_fixture.py
"""

import json
import pathlib
import re
import sys

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "skill_block.json"
FIELDS = {"current", "minimum", "yours", "status", "updateUrl"}
STATUSES = {"current", "behind", "unsupported", "unknown"}
VERSION = re.compile(r"^\d{1,3}\.\d{1,3}(\.\d{1,3})?$")

problems = []
block = json.loads(FIXTURE.read_text())
if set(block) != {"skill"} or not isinstance(block["skill"], dict):
    problems.append("the file holds one key, skill, whose value is an object")
else:
    skill = block["skill"]
    if set(skill) != FIELDS:
        problems.append(f"skill has exactly {sorted(FIELDS)}, not {sorted(skill)}")
    if any(not isinstance(value, str) for value in skill.values()):
        problems.append("every skill value is a string, so the block stays flat")
    if skill.get("status") not in STATUSES:
        problems.append(f"status is one of {sorted(STATUSES)}")
    for field in ("current", "minimum", "yours"):
        if not VERSION.match(str(skill.get(field, ""))):
            problems.append(f"{field} is MAJOR.MINOR or MAJOR.MINOR.PATCH")
    if not str(skill.get("updateUrl", "")).startswith("https://"):
        problems.append("updateUrl is an https URL")

for problem in problems:
    print(f"{FIXTURE.name}: {problem}")
sys.exit(1 if problems else 0)
