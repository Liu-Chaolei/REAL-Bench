"""Task-scoped retail background for the simulated caller's recognition."""

import re
import unicodedata

from tau2.data_model.asr import HotwordCandidate, HotwordMaterials
from tau2.data_model.tasks import StructuredUserInstructions


def user_asr_materials(db, task) -> HotwordMaterials:
    """Resolve one user from stated identity; never inspect reference actions."""
    instructions = task.user_scenario.instructions
    text = (
        instructions.reason_for_call + "\n" + (instructions.known_info or "")
        if isinstance(instructions, StructuredUserInstructions)
        else instructions
    )
    text = unicodedata.normalize("NFC", text).casefold()
    matches = []
    for person in db.users.values():
        name = unicodedata.normalize(
            "NFC", f"{person.name.first_name} {person.name.last_name}"
        ).casefold()
        if re.search(
            r"(?<!\w)" + re.escape(person.user_id.casefold()) + r"(?!\w)", text
        ) or re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text):
            matches.append(person)
    if len(matches) > 1:
        matches = [person for person in matches if person.address.zip in text]
    if len(matches) != 1:
        return HotwordMaterials()
    person = matches[0]
    terms = [
        HotwordCandidate(
            text=f"{person.name.first_name} {person.name.last_name}",
            source=f"retail.users.{person.user_id}.name",
            priority=1,
        )
    ]
    for field in ("city", "country", "state", "address1"):
        value = getattr(person.address, field)
        if value:
            terms.append(
                HotwordCandidate(
                    text=value, source=f"retail.users.{person.user_id}.address.{field}"
                )
            )
    for identifier in person.orders:
        order = db.orders.get(identifier)
        if order is None or order.user_id != person.user_id:
            continue
        for item in order.items:
            terms.append(
                HotwordCandidate(
                    text=item.name, source=f"retail.orders.{identifier}.items.name"
                )
            )
    return HotwordMaterials(candidates=tuple(terms))
