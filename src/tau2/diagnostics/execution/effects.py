"""Business postconditions from executing application state, with per-call evidence."""

from __future__ import annotations

import ast
import base64
import hashlib
import math
import posixpath

from tau2.diagnostics.execution.bindings import TOKEN, ReferenceBindings
from tau2.diagnostics.execution.checks import (
    POLICY,
    compare_args,
    hard_check,
)
from tau2.diagnostics.execution.scoring import (
    dependency_edges,
    dimension,
    topological_operations,
)

from .state import app_state, find_record, record_identity, state_datetime_to_api


def verdict(values):
    return False if False in values else None if None in values or not values else True


def check(field, passed, reason="actual_app_state"):
    return {"field": field, "passed": passed, "reason": reason}


def path_key(path):
    return posixpath.normpath(path).lstrip("/")


def tree_paths(state):
    if not isinstance(state, dict) or not isinstance(state.get("files"), dict):
        return None
    paths = {}

    def walk(node, parent=""):
        path = posixpath.join(parent, node.get("name", ""))
        paths[path_key(path)] = node.get("type")
        for child in node.get("children", []):
            walk(child, path)

    walk(state["files"])
    return paths


def quantities(cart):
    if not isinstance(cart, dict):
        return None
    return {
        key: row.get("quantity") for key, row in cart.items() if isinstance(row, dict)
    }


def folder(state, name):
    if not isinstance(state, dict) or not isinstance(state.get("folders"), dict):
        return None
    return state["folders"].get(str(name).upper())


class EffectEvaluator:
    HANDLERS = {
        "SandboxLocalFileSystem__open": "open_file",
        "ApartmentListingApp__save_apartment": "saved",
        "ApartmentListingApp__remove_saved_apartment": "saved",
        "CalendarApp__add_calendar_event": "create_record",
        "CalendarApp__delete_calendar_event": "delete_record",
        "ContactsApp__add_new_contact": "create_record",
        "ContactsApp__delete_contact": "delete_record",
        "ContactsApp__edit_contact": "edit_contact",
        "EmailClientApp__send_email": "mail",
        "EmailClientApp__reply_to_email": "mail",
        "EmailClientApp__forward_email": "mail",
        "EmailClientApp__move_email": "move_mail",
        "EmailClientApp__delete_email": "delete_record",
        "MessagingAppV2__send_message": "message",
        "MessagingAppV2__send_message_to_group_conversation": "message",
        "MessagingAppV2__create_group_conversation": "group",
        "MessagingAppV2__add_participant_to_conversation": "group",
        "MessagingAppV2__remove_participant_from_conversation": "group",
        "MessagingAppV2__change_conversation_title": "group",
        "ShoppingApp__add_to_cart": "cart",
        "ShoppingApp__remove_from_cart": "cart",
        "ShoppingApp__checkout": "checkout",
        "ShoppingApp__cancel_order": "cancel_order",
        "CabApp__order_ride": "ride",
        "CabApp__user_cancel_ride": "cancel_ride",
        "SandboxLocalFileSystem__mkdir": "files",
        "SandboxLocalFileSystem__mv": "files",
        "SandboxLocalFileSystem__rm": "files",
    }

    def __init__(self, grader, reference, actual, matching):
        self.grader = grader
        self.reference = reference
        self.actual = actual
        self.matching = matching
        self.bindings = ReferenceBindings(reference, actual, matching)
        self.edges = dependency_edges(
            grader.scenario["events"], {r["id"] for r in reference}
        )
        self.reference_order = topological_operations(reference, self.edges)

    def evaluate(self):
        units = []
        for index, operation in enumerate(self.reference):
            choices = self.matching["alternatives"].get(index, [None])
            unit = {
                "reference_id": operation["id"],
                "tool": operation["tool"],
                "passed": None,
            }
            if choices == [None]:
                unit.update(
                    passed=False, reason="required_operation_not_performed", checks=[]
                )
            elif len(choices) != 1 or choices[0] is None:
                unit.update(reason="ambiguous_operation_matching", checks=[])
            else:
                call = self.actual[choices[0]]
                args, errors = self.bindings.arguments(operation, call)
                if call["succeeded"] is False:
                    unit.update(
                        passed=False, reason="required_operation_failed", checks=[]
                    )
                elif errors:
                    unit.update(
                        passed=verdict([v["passed"] for v in errors.values()]),
                        reason="reference_binding_unavailable",
                        checks=[
                            check(k, v["passed"], v["reason"])
                            for k, v in errors.items()
                        ],
                    )
                else:
                    method = self.HANDLERS.get(operation["tool"])
                    before, after = self.grader.phase_frames(choices[0])
                    name = self.grader.app_name(operation)
                    context = {
                        "index": index,
                        "operation": operation,
                        "args": args,
                        "call": call,
                        "before_frame": before,
                        "after_frame": after,
                        "name": name,
                        "before": app_state(before, name),
                        "after": app_state(after, name),
                        "initial": app_state(
                            self.grader.runtime.state_recorder.initial, name
                        ),
                        "final": app_state(after, name),
                    }
                    try:
                        checks = (
                            getattr(self, method)(context)
                            if method
                            else [
                                check(
                                    "effect",
                                    None,
                                    "effect_validator_not_implemented_for_tool",
                                )
                            ]
                        )
                    except (KeyError, TypeError, ValueError, AttributeError) as exc:
                        checks = [
                            check(
                                "state",
                                None,
                                "state_schema_unavailable:" + type(exc).__name__,
                            )
                        ]
                    unit.update(
                        passed=verdict([c["passed"] for c in checks]),
                        reason="executing_app_postconditions",
                        checks=checks,
                        actual_id=call["id"],
                        state_frames=[
                            f.get("index") if f else None for f in (before, after)
                        ],
                    )
            units.append(unit)
        return dimension(units)

    def planned(self, c, function, field=None, target=None):
        for operation in self.reference:
            if self.grader.app_name(operation) != c["name"] or not operation[
                "tool"
            ].endswith("__" + function):
                continue
            value = operation["args"].get(field) if field else None
            if (
                field is None
                or value == target
                or value == "{{" + c["operation"]["id"] + "}}"
            ):
                return True
        return False

    def record_checks(self, c, record, fields=None):
        if not isinstance(record, dict):
            return [check("record", False, "required_record_absent")]
        expected = (
            c["args"]
            if fields is None
            else {k: v for k, v in c["args"].items() if k in fields}
        )
        tool = c["operation"]["tool"]
        values = dict(record)
        for name in ("start_datetime", "end_datetime"):
            if name in values:
                values[name] = state_datetime_to_api(values[name])
        if tool.startswith("CabApp"):
            values["ride_time"] = state_datetime_to_api(record.get("time_stamp"))
        checks = compare_args(tool, values, expected, self.grader.user_name)
        original = self.matching["checks"].get(c["index"], [])
        result = []
        for row in checks:
            name = row["parameter"]
            if name == "$semantic_group":
                if row["passed"] is None:
                    stored_matches_call = all(
                        values.get(f) == c["call"]["args"].get(f) for f in row["fields"]
                    )
                    cached = next(
                        (
                            x
                            for x in original
                            if x["parameter"] == name
                            and set(x.get("fields", [])) == set(row["fields"])
                        ),
                        {},
                    )
                    if stored_matches_call:
                        row["passed"] = cached.get("passed")
                    else:
                        # These pinned app methods store the supplied text verbatim.
                        row["passed"] = False
                result.append(
                    check(name, row["passed"], "stored_text_and_native_semantic_check")
                )
            elif name in expected:
                if (
                    tool.startswith("CabApp")
                    and name == "ride_time"
                    and expected[name] is None
                ):
                    continue
                result.append(check(name, row["passed"]))
        return result

    def created(self, c, collection):
        receipt = record_identity(c["call"].get("return_value"))
        if not receipt:
            return None, [check("receipt", None, "creation_receipt_unavailable")]
        if c["after"] is None or c["before"] is None:
            return None, [check("state", None, "operation_phase_state_unavailable")]
        if find_record(c["before"].get(collection), receipt) is not None:
            return None, [
                check("new_record", False, "preexisting_record_is_not_new_creation")
            ]
        record = find_record(c["after"].get(collection), receipt)
        return record, [check("new_record", record is not None)]

    def saved(self, c):
        wanted = c["operation"]["tool"].endswith("__save_apartment")
        opposite = "remove_saved_apartment" if wanted else "save_apartment"
        state = (
            c["after"]
            if self.planned(c, opposite, "apartment_id", c["args"]["apartment_id"])
            else c["final"]
        )
        saved = state.get("saved_apartments") if state else None
        return [
            check(
                "saved_apartments",
                (c["args"]["apartment_id"] in saved) == wanted
                if isinstance(saved, list)
                else None,
            )
        ]

    def create_record(self, c):
        calendar = c["operation"]["tool"].startswith("CalendarApp")
        collection = "events" if calendar else "contacts"
        record, checks = self.created(c, collection)
        if record is None:
            return checks
        checks += self.record_checks(c, record)
        function, field = (
            ("delete_calendar_event", "event_id")
            if calendar
            else ("delete_contact", "contact_id")
        )
        if not self.planned(
            c, function, field, record_identity(c["call"].get("return_value"))
        ):
            final = (
                find_record(
                    c["final"].get(collection),
                    record_identity(c["call"].get("return_value")),
                )
                if c["final"]
                else None
            )
            checks += (
                self.record_checks(c, final)
                if c["final"]
                else [check("final_state", None)]
            )
        return checks

    def delete_record(self, c):
        tool = c["operation"]["tool"]
        field, collection = (
            ("event_id", "events")
            if tool.startswith("Calendar")
            else ("contact_id", "contacts")
            if tool.startswith("Contacts")
            else ("email_id", "folders")
        )
        target = c["args"][field]
        before = c["before"]
        final = c["final"]
        if before is None or final is None:
            return [check("state", None, "phase_or_final_state_unavailable")]
        source = (
            folder(before, c["args"].get("folder_name", "INBOX"))
            if collection == "folders"
            else before.get(collection)
        )
        if source is None:
            return [check("source", None, "source_collection_unavailable")]
        checks = [check("source_record", find_record(source, target) is not None)]
        remaining = (
            {k: v for k, v in final.get("folders", {}).items() if k != "TRASH"}
            if collection == "folders"
            else final.get(collection)
        )
        checks.append(
            check(
                "target_absent",
                find_record(remaining, target) is None
                if remaining is not None
                else None,
            )
        )
        return checks

    def edit_contact(self, c):
        if c["after"] is None or c["final"] is None:
            return [check("state", None)]
        target = c["args"]["contact_id"]
        record = find_record(c["after"].get("contacts"), target)
        final = find_record(c["final"].get("contacts"), target)
        checks = []
        for field, expected in c["args"]["updates"].items():

            def field_matches(value):
                if (
                    field in {"gender", "status"}
                    and isinstance(value, str)
                    and isinstance(expected, str)
                ):
                    return (
                        value.split(".")[-1].casefold()
                        == expected.split(".")[-1].casefold()
                    )
                checker = POLICY["tool_args"]["ContactsApp__add_new_contact"].get(
                    field, "eq_checker"
                )
                return hard_check(checker, value, expected, self.grader.user_name)

            checks.append(
                check(
                    "updates." + field,
                    field_matches(record.get(field)) if record else False,
                )
            )
            later = any(
                r["id"] != c["operation"]["id"]
                and r["tool"] == c["operation"]["tool"]
                and r["args"].get("contact_id") == target
                and field in r["args"].get("updates", {})
                for r in self.reference
            )
            if not later and not self.planned(
                c, "delete_contact", "contact_id", target
            ):
                checks.append(
                    check(
                        "final." + field,
                        field_matches(final.get(field)) if final else False,
                    )
                )
        return checks

    def attachment_checks(self, c, attachments, paths):
        if not isinstance(attachments, dict):
            return [
                check("attachments", False if attachments is None and paths else None)
            ]
        names = {posixpath.basename(path) for path in paths}
        checks = [check("attachment_names", set(attachments) == names)]
        for path in paths:
            source = (c["before_frame"] or {}).get("file_evidence", {})
            evidence = [
                data.get(path_key(path))
                for data in source.values()
                if path_key(path) in data
            ]
            digests = {entry.get("sha256") for entry in evidence if entry.get("sha256")}
            content = attachments.get(posixpath.basename(path))
            if content is None:
                checks.append(check("attachment:" + path, False))
            elif len(digests) != 1:
                checks.append(
                    check("attachment:" + path, None, "source_file_digest_unavailable")
                )
            else:
                try:
                    if isinstance(content, str) and content.startswith(("b'", 'b"')):
                        # Pinned ARE serializes bytes with str(bytes), preserving base64 inside.
                        content = ast.literal_eval(content)
                    digest = hashlib.sha256(
                        base64.b64decode(content, validate=True)
                    ).hexdigest()
                    checks.append(
                        check(
                            "attachment:" + path,
                            digest in digests,
                            "attachment_bytes_against_source_digest",
                        )
                    )
                except (ValueError, TypeError):
                    checks.append(
                        check(
                            "attachment:" + path,
                            None,
                            "attachment_encoding_unavailable",
                        )
                    )
        return checks

    def mail(self, c):
        record, checks = self.created(c, "folders")
        if record is None:
            return checks
        receipt = record_identity(c["call"].get("return_value"))
        checks.append(
            check(
                "sent_folder",
                find_record(folder(c["after"], "SENT"), receipt) is not None,
            )
        )
        function = c["operation"]["tool"].split("__")[1]
        fields = (
            {"recipients", "cc", "subject", "content"}
            if function == "send_email"
            else {"content"}
            if function == "reply_to_email"
            else {"recipients"}
        )
        checks += self.record_checks(c, record, fields)
        if function in {"reply_to_email", "forward_email"}:
            source = find_record(
                folder(c["before"], c["args"].get("folder_name", "INBOX")),
                c["args"]["email_id"],
            )
            checks.append(check("source_email", source is not None))
            checks.append(
                check("parent_id", record.get("parent_id") == c["args"]["email_id"])
            )
            if source:
                prefix = "Re: " if function == "reply_to_email" else "FWD: "
                checks.append(
                    check(
                        "subject",
                        record.get("subject") == prefix + source.get("subject", ""),
                    )
                )
                if function == "forward_email":
                    checks += [
                        check(
                            "forward_content",
                            record.get("content") == "> " + source.get("content", ""),
                        ),
                        check(
                            "forward_attachments",
                            record.get("attachments") == source.get("attachments"),
                        ),
                    ]
                else:
                    seen = set()
                    while (
                        source.get("sender") == c["before"].get("user_email")
                        and source.get("parent_id")
                        and source["parent_id"] not in seen
                    ):
                        seen.add(source["parent_id"])
                        source = find_record(
                            c["before"].get("folders"), source["parent_id"]
                        )
                        if not source:
                            break
                    checks.append(
                        check(
                            "reply_recipient",
                            record.get("recipients") == [source["sender"]]
                            if source
                            else None,
                        )
                    )
        if function != "forward_email":
            checks += self.attachment_checks(
                c,
                record.get("attachments") or {},
                c["args"].get("attachment_paths") or [],
            )
        if not self.planned(
            c, "delete_email", "email_id", receipt
        ) and not self.planned(c, "move_email", "email_id", receipt):
            checks.append(
                check(
                    "final_sent_record",
                    find_record(folder(c["final"], "SENT"), receipt) is not None
                    if c["final"]
                    else None,
                )
            )
        return checks

    def move_mail(self, c):
        if c["before"] is None or c["after"] is None:
            return [check("state", None)]
        args = c["args"]
        target = args["email_id"]
        before = find_record(folder(c["before"], args["source_folder_name"]), target)
        after = find_record(folder(c["after"], args["dest_folder_name"]), target)
        checks = [
            check("source_email", before is not None),
            check("destination_email", after is not None),
            check("preserved_email", after == before),
        ]
        if args["source_folder_name"].upper() != args["dest_folder_name"].upper():
            checks.append(
                check(
                    "source_removed",
                    find_record(folder(c["after"], args["source_folder_name"]), target)
                    is None,
                )
            )
        if (
            not self.planned(c, "delete_email", "email_id", target)
            and sum(
                r["tool"] == c["operation"]["tool"]
                and r["args"].get("email_id") == target
                for r in self.reference
            )
            == 1
        ):
            checks.append(
                check(
                    "final_destination",
                    find_record(folder(c["final"], args["dest_folder_name"]), target)
                    is not None
                    if c["final"]
                    else None,
                )
            )
        return checks

    def message(self, c):
        if c["before"] is None or c["after"] is None:
            return [check("state", None)]
        conversation_id = record_identity(c["call"].get("return_value"))
        conversation = find_record(c["after"].get("conversations"), conversation_id)
        previous = find_record(c["before"].get("conversations"), conversation_id) or {}
        if not conversation:
            return [check("conversation", False if conversation_id else None)]
        old_ids = {row.get("message_id") for row in previous.get("messages", [])}
        messages = [
            row
            for row in conversation.get("messages", [])
            if row.get("message_id") not in old_ids
        ]
        if len(messages) != 1:
            return [
                check(
                    "new_message", False if not messages else None, "new_message_diff"
                )
            ]
        message = messages[0]
        checks = [
            check(
                "sender", message.get("sender_id") == c["after"].get("current_user_id")
            )
        ]
        if "user_id" in c["args"]:
            checks.append(
                check(
                    "recipient",
                    set(conversation.get("participant_ids", []))
                    == {c["args"]["user_id"], c["after"].get("current_user_id")},
                )
            )
        else:
            checks.append(
                check(
                    "conversation_id", conversation_id == c["args"]["conversation_id"]
                )
            )
            expected_members = self.expected_group_members(c)
            checks.append(
                check(
                    "recipient_members",
                    set(conversation.get("participant_ids", [])) == expected_members
                    if expected_members is not None
                    else None,
                    "reference_conversation_membership",
                )
            )
        checks += self.record_checks(c, message, {"content"})
        attachment = c["args"].get("attachment_path")
        payload = (
            {message.get("attachment_name"): message.get("attachment")}
            if message.get("attachment") is not None
            else {}
        )
        checks += self.attachment_checks(c, payload, [attachment] if attachment else [])
        final = (
            find_record(c["final"].get("conversations"), conversation_id)
            if c["final"]
            else None
        )
        checks.append(
            check(
                "final_message",
                find_record(final.get("messages"), message.get("message_id"))
                is not None
                if final
                else False
                if c["final"]
                else None,
            )
        )
        return checks

    def expected_group_members(self, c):
        if getattr(self.grader, "dynamic_membership", False):
            # Environment-owned membership changes require observed causal state;
            # a static oracle membership set cannot establish a failure.
            return None
        raw_target = c["operation"]["args"].get("conversation_id")
        binding = TOKEN.fullmatch(raw_target) if isinstance(raw_target, str) else None
        if binding:
            source = next(
                (row for row in self.reference if row["id"] == binding[1]), None
            )
            if source is None or not source["tool"].endswith(
                "__create_group_conversation"
            ):
                return None
            members = set(source["args"]["user_ids"]) | {
                c["after"].get("current_user_id")
            }
        else:
            source = find_record((c["initial"] or {}).get("conversations"), raw_target)
            if source is None:
                return None
            members = set(source.get("participant_ids", []))
        ancestors = {c["operation"]["id"]}
        while True:
            expanded = ancestors | {a for a, b in self.edges if b in ancestors}
            if expanded == ancestors:
                break
            ancestors = expanded
        for row in self.reference_order:
            if (
                row["id"] not in ancestors
                or row["args"].get("conversation_id") != raw_target
                or self.grader.app_name(row) != c["name"]
            ):
                continue
            if row["tool"].endswith("__add_participant_to_conversation"):
                members.add(row["args"]["user_id"])
            elif row["tool"].endswith("__remove_participant_from_conversation"):
                members.discard(row["args"]["user_id"])
        return members

    def group(self, c):
        function = c["operation"]["tool"].split("__")[1]
        target = c["args"].get("conversation_id") or record_identity(
            c["call"].get("return_value")
        )
        state = c["after"]
        if state is None:
            return [check("state", None)]
        record = find_record(state.get("conversations"), target)
        if not record:
            return [check("conversation", False)]
        if function == "create_group_conversation":
            old = find_record((c["before"] or {}).get("conversations"), target)
            checks = [
                check("new_group", old is None if c["before"] else None),
                check(
                    "participants",
                    set(record.get("participant_ids", []))
                    == set(c["args"]["user_ids"]) | {state.get("current_user_id")},
                ),
            ]
            checks += self.record_checks(c, record, {"title"})
            has_changes = any(
                self.planned(c, fn, "conversation_id", target)
                for fn in (
                    "add_participant_to_conversation",
                    "remove_participant_from_conversation",
                    "change_conversation_title",
                )
            )
            if not has_changes:
                final = find_record((c["final"] or {}).get("conversations"), target)
                checks.append(
                    check(
                        "final_group",
                        final is not None
                        and set(final.get("participant_ids", []))
                        == set(record.get("participant_ids", []))
                        and final.get("title") == record.get("title")
                        if c["final"]
                        else None,
                    )
                )
            return checks
        if function == "change_conversation_title":
            checks = self.record_checks(c, record, {"title"})
        else:
            wanted = function == "add_participant_to_conversation"
            checks = [
                check(
                    "participant",
                    (c["args"]["user_id"] in record.get("participant_ids", []))
                    == wanted,
                )
            ]
        other_changes = sum(
            r["tool"].startswith("MessagingAppV2__")
            and r["args"].get("conversation_id")
            == c["operation"]["args"].get("conversation_id")
            and r["id"] != c["operation"]["id"]
            and r["tool"].split("__")[1]
            in {
                "add_participant_to_conversation",
                "remove_participant_from_conversation",
                "change_conversation_title",
            }
            for r in self.reference
        )
        if not other_changes:
            final = find_record((c["final"] or {}).get("conversations"), target)
            checks.append(
                check(
                    "final_group_requirement",
                    final.get("title") == record.get("title")
                    if function == "change_conversation_title" and final
                    else (
                        (c["args"]["user_id"] in final.get("participant_ids", []))
                        == wanted
                    )
                    if final
                    else False
                    if c["final"]
                    else None,
                )
            )
        return checks

    def expected_cart(self, c):
        initial = c["initial"]
        cart = quantities(initial.get("cart")) if initial else None
        if cart is None:
            return None
        ancestors = {c["operation"]["id"]}
        while True:
            expanded = ancestors | {a for a, b in self.edges if b in ancestors}
            if expanded == ancestors:
                break
            ancestors = expanded
        for operation in self.reference_order:
            if operation["id"] == c["operation"]["id"]:
                break
            if (
                self.grader.app_name(operation) != c["name"]
                or operation["id"] not in ancestors
            ):
                continue
            args = operation["args"]
            if operation["tool"].endswith("__checkout"):
                cart = {}
            elif operation["tool"].endswith(("__add_to_cart", "__remove_from_cart")):
                if not isinstance(args.get("item_id"), str) or "{{" in args["item_id"]:
                    return None
                sign = 1 if operation["tool"].endswith("__add_to_cart") else -1
                cart[args["item_id"]] = (
                    cart.get(args["item_id"], 0) + sign * args["quantity"]
                )
                if cart[args["item_id"]] == 0:
                    del cart[args["item_id"]]
        return cart

    def cart(self, c):
        if c["before"] is None or c["after"] is None:
            return [check("state", None)]
        target, quantity = c["args"]["item_id"], c["args"]["quantity"]
        before, after = (
            quantities(c["before"].get("cart")),
            quantities(c["after"].get("cart")),
        )
        if before is None or after is None:
            return [check("cart", None)]
        sign = 1 if c["operation"]["tool"].endswith("__add_to_cart") else -1
        expected = before.get(target, 0) + sign * quantity
        checks = [check("cart_quantity_delta", after.get(target, 0) == expected)]
        if not self.planned(c, "checkout") and not self.planned(
            c, "remove_from_cart" if sign > 0 else "add_to_cart", "item_id", target
        ):
            total = quantities(c["initial"].get("cart")) if c["initial"] else None
            final = quantities(c["final"].get("cart")) if c["final"] else None
            if total is not None:
                wanted = total.get(target, 0) + sum(
                    (1 if r["tool"].endswith("__add_to_cart") else -1)
                    * r["args"]["quantity"]
                    for r in self.reference
                    if r["tool"].endswith(("__add_to_cart", "__remove_from_cart"))
                    and r["args"].get("item_id") == target
                )
                checks.append(
                    check(
                        "final_cart_quantity",
                        final.get(target, 0) == wanted if final is not None else None,
                    )
                )
        return checks

    def checkout(self, c):
        record, checks = self.created(c, "orders")
        if record is None:
            return checks
        expected = self.expected_cart(c)
        actual = quantities(record.get("order_items"))
        checks += [
            check(
                "order_items",
                actual == expected
                if expected is not None and actual is not None
                else None,
            ),
            check("order_status", record.get("order_status") == "processed"),
            check("cart_cleared", c["after"].get("cart") == {}),
        ]
        total, valid = 0, expected is not None
        for item, quantity in (expected or {}).items():
            product = find_record(c["before"].get("products"), item)
            product = product or find_record(c["before"].get("cart"), item)
            price = product.get("price") if product else None
            code = c["args"].get("discount_code")
            discount = (
                c["before"].get("discount_codes", {}).get(item, {}).get(code)
                if code
                else 0
            )
            if not isinstance(price, (int, float)) or not isinstance(
                discount, (int, float)
            ):
                valid = False
            else:
                total += price * quantity * (1 - discount / 100)
        checks.append(
            check(
                "order_total",
                math.isclose(record.get("order_total"), total, abs_tol=1e-8)
                if valid and isinstance(record.get("order_total"), (int, float))
                else None,
            )
        )
        receipt = record_identity(c["call"].get("return_value"))
        if not self.planned(c, "cancel_order", "order_id", receipt):
            final = find_record((c["final"] or {}).get("orders"), receipt)
            checks.append(
                check(
                    "final_order",
                    final is not None
                    and final.get("order_status") == "processed"
                    and final.get("order_items") == record.get("order_items")
                    if c["final"]
                    else None,
                )
            )
        return checks

    def cancel_order(self, c):
        target = c["args"]["order_id"]
        before = find_record((c["before"] or {}).get("orders"), target)
        after = find_record((c["after"] or {}).get("orders"), target)
        return [
            check(
                "cancellable_order",
                before.get("order_status") in {"processed", "shipped"}
                if before
                else False
                if c["before"]
                else None,
            ),
            check(
                "cancelled_order",
                after.get("order_status") == "cancelled"
                if after
                else False
                if c["after"]
                else None,
            ),
        ]

    def ride(self, c):
        record, checks = self.created(c, "ride_history")
        if record is None:
            return checks
        checks += self.record_checks(c, record)
        checks.append(check("booked", record.get("status") == "BOOKED"))
        if not self.planned(c, "user_cancel_ride"):
            final = find_record(
                (c["final"] or {}).get("ride_history"), record.get("ride_id")
            )
            checks.append(
                check(
                    "final_ride",
                    final is not None
                    and final.get("status")
                    in {
                        "BOOKED",
                        "ARRIVED_AT_PICKUP",
                        "IN_PROGRESS",
                        "COMPLETED",
                        "DELAYED",
                    }
                    if c["final"]
                    else None,
                )
            )
        return checks

    def cancel_ride(self, c):
        before = (c["before"] or {}).get("ride_history")
        after = (c["after"] or {}).get("ride_history")
        if not isinstance(before, list) or not before or not isinstance(after, list):
            return [
                check(
                    "ride_history",
                    None if c["before"] is None or c["after"] is None else False,
                )
            ]
        record = find_record(after, before[-1].get("ride_id"))
        return [
            check(
                "cancelled_ride",
                record is not None
                and record.get("status") == "CANCELLED"
                and before[-1].get("status") != "CANCELLED",
            )
        ]

    def open_file(self, c):
        target = path_key(c["args"]["path"])
        state = (c["after_frame"] or {}).get("file_evidence", {}).get(c["name"], {})
        actual = state.get(target, {}).get("sha256")
        if actual is None:
            return [check("file_exists", False if c["after"] else None)]
        mode = c["args"].get("mode", "rb")
        kwargs = c["args"].get("kwargs") or {}
        content = kwargs.get("data", kwargs.get("content"))
        if isinstance(content, str):
            return [
                check(
                    "file_content",
                    actual == hashlib.sha256(content.encode()).hexdigest(),
                    "reference_content_vs_executed_file_bytes",
                )
            ]
        if "w" in mode:
            return [check("file_truncated", actual == hashlib.sha256(b"").hexdigest())]
        return [check("file_exists", True)]

    def files(self, c):
        before, after = tree_paths(c["before"]), tree_paths(c["after"])
        if before is None or after is None:
            return [check("file_tree", None)]
        function = c["operation"]["tool"].split("__")[1]
        args = c["args"]
        if function == "mkdir":
            target = path_key(args["path"])
            checks = [check("directory_created", after.get(target) == "directory")]
        elif function == "rm":
            target = path_key(args["path"])
            checks = [
                check("source_exists", target in before),
                check(
                    "path_removed",
                    not any(p == target or p.startswith(target + "/") for p in after),
                ),
            ]
        else:
            source, target = path_key(args["path1"]), path_key(args["path2"])
            # fsspec appends the source basename when moving into an existing directory.
            if before.get(target) == "directory":
                target = posixpath.join(target, posixpath.basename(source))
            subtree = {
                p[len(source) :]: kind
                for p, kind in before.items()
                if p == source or p.startswith(source + "/")
            }
            checks = [
                check("source_exists", bool(subtree)),
                check(
                    "source_removed",
                    not any(p == source or p.startswith(source + "/") for p in after),
                ),
                check(
                    "destination_structure",
                    all(
                        after.get(target + suffix) == kind
                        for suffix, kind in subtree.items()
                    ),
                ),
            ]
            for suffix, kind in subtree.items():
                if kind != "file":
                    continue
                old = (
                    (c["before_frame"] or {})
                    .get("file_evidence", {})
                    .get(c["name"], {})
                    .get(source + suffix, {})
                )
                new = (
                    (c["after_frame"] or {})
                    .get("file_evidence", {})
                    .get(c["name"], {})
                    .get(target + suffix, {})
                )
                checks.append(
                    check(
                        "file_content_preserved:" + suffix,
                        old["sha256"] == new["sha256"]
                        if old.get("sha256") and new.get("sha256")
                        else None,
                        "file_digest_comparison",
                    )
                )
        final = tree_paths(c["final"])
        changed_later = any(
            r["id"] != c["operation"]["id"]
            and r["tool"].startswith("SandboxLocalFileSystem__")
            and any(
                path_key(v) == target or path_key(v).startswith(target + "/")
                for k, v in r["args"].items()
                if k in {"path", "path1", "path2"} and isinstance(v, str)
            )
            for r in self.reference
        )
        if not changed_later:
            checks.append(
                check(
                    "final_path",
                    (target not in final if function == "rm" else target in final)
                    if final is not None
                    else None,
                )
            )
        return checks
