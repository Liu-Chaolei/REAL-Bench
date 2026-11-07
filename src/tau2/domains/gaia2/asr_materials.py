"""Literal entities from the caller's initial personal applications."""

from email.utils import parseaddr

from tau2.data_model.asr import HotwordCandidate, HotwordMaterials


def user_asr_materials(snapshot: list[dict]) -> HotwordMaterials:
    """Collect personal entities without reading references or answers."""
    terms = []

    def add(value, source, priority=2):
        if isinstance(value, str) and value.strip():
            terms.append(HotwordCandidate(text=value, source=source, priority=priority))

    for app in snapshot:
        name, state = app["name"], app["app_state"]
        if name == "Contacts":
            for identifier, person in state.get("contacts", {}).items():
                source = f"Contacts.contacts.{identifier}"
                add(
                    " ".join(
                        str(person.get(k) or "") for k in ("first_name", "last_name")
                    ),
                    source + ".name",
                    1,
                )
                for field in ("city_living", "country", "address"):
                    add(person.get(field), source + "." + field)
        elif name in {"Messages", "Chats"}:
            for person in state.get("name_to_id", {}):
                add(person, f"{name}.participants", 1)
            for identifier, conversation in state.get("conversations", {}).items():
                add(
                    conversation.get("title"),
                    f"{name}.conversations.{identifier}.title",
                )
        elif name == "Calendar":
            for identifier, event in state.get("events", {}).items():
                source = f"Calendar.events.{identifier}"
                for person in event.get("attendees", []):
                    add(person, source + ".attendees", 1)
                for field in ("title", "location"):
                    add(event.get(field), source + "." + field)
        elif name == "Emails":
            for folder in state.get("folders", {}).values():
                emails = folder.get("emails", [])
                rows = emails.values() if isinstance(emails, dict) else emails
                for email in rows:
                    addresses = [email.get("sender", "")]
                    addresses.extend(email.get("recipients", []))
                    addresses.extend(email.get("cc", []))
                    for address in addresses:
                        if isinstance(address, str):
                            display, _ = parseaddr(address)
                            add(display, "Emails.participant_names", 1)
        elif name == "Shopping":
            for identifier, product in state.get("products", {}).items():
                add(product.get("name"), f"Shopping.products.{identifier}.name")
        elif name == "RentAFlat":
            for identifier, apartment in state.get("apartments", {}).items():
                for field in ("name", "location"):
                    add(
                        apartment.get(field),
                        f"RentAFlat.apartments.{identifier}.{field}",
                    )
        elif name == "City":
            for location in state.get("crime_data", {}):
                add(location, "City.locations")
    return HotwordMaterials(candidates=tuple(terms))
