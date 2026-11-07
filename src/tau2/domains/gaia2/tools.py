"""Gaia2 application methods exposed through the standard tau2 toolkit."""

from __future__ import annotations

import base64
import copy
import hashlib
import inspect
import json
import random
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from tau2.config import DEFAULT_SCENARIO_NOTIFICATION_WAIT_SECONDS
from tau2.domains.gaia2.data_model import Gaia2DB
from tau2.environment.tool import Tool
from tau2.environment.toolkit import ToolKitBase, ToolType, is_tool


def json_value(value):
    """Store application results as replayable JSON, not Python object handles."""
    from tau2.domains.gaia2.vendor.utils import serialize_field

    value = serialize_field(value)

    def encode(item):
        if isinstance(item, bytes):
            try:
                return item.decode("utf-8")
            except UnicodeDecodeError:
                return {
                    "encoding": "base64",
                    "data": base64.b64encode(item).decode("ascii"),
                }
        return str(item)

    if isinstance(value, bytes):
        return encode(value)
    if hasattr(value, "read"):
        value.close()
        return "File opened; use Files__cat or Files__read_document to read content."
    return json.loads(json.dumps(value, default=encode, ensure_ascii=False))


@lru_cache(maxsize=4096)
def _file_digest(path: str, size: int, modified_ns: int) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def location_aliases(snapshot):
    """Resolve aliases explicitly present in the scenario's business data."""
    aliases = {}
    for app in snapshot:
        if app["name"] == "Contacts":
            for person in app["app_state"].get("contacts", {}).values():
                if person.get("is_user") and person.get("address"):
                    aliases["home"] = person["address"]
        if app["name"] == "RentAFlat":
            for apartment in app["app_state"].get("apartments", {}).values():
                name = apartment.get("name")
                address = apartment.get("address")
                if name and address:
                    aliases[" ".join(name.split()).casefold()] = address
                    aliases[" ".join(f"{name}, {address}".split()).casefold()] = address
    return aliases


def normalize_arguments(name, arguments, aliases):
    """Normalize collection defaults and known address aliases for causal anchors."""
    result = copy.deepcopy(arguments)
    for key in (
        "attendees",
        "cc",
        "recipients",
        "participant_ids",
        "participants",
        "user_ids",
        "attachment_paths",
        "attachments",
    ):
        if key in result and result[key] is None:
            result[key] = []
        if isinstance(result.get(key), list):
            result[key] = sorted(
                result[key], key=lambda item: json.dumps(item, sort_keys=True)
            )
    if name.startswith("Cabs__"):
        for key in ("start_location", "end_location"):
            if isinstance(result.get(key), str):
                normalized = " ".join(result[key].split()).casefold()
                result[key] = aliases.get(normalized, result[key])
    return result


class Gaia2Tools(ToolKitBase):
    """Adapt application semantics; the shared runtime owns time and events."""

    def __init__(self, snapshot: list[dict], *, assets: Path, clock=None, seed=0):
        from tau2.domains.gaia2.vendor.apps.apartment_listing import RentAFlat
        from tau2.domains.gaia2.vendor.apps.app import Protocol
        from tau2.domains.gaia2.vendor.apps.cab import CabApp
        from tau2.domains.gaia2.vendor.apps.calendar import Calendar
        from tau2.domains.gaia2.vendor.apps.city import CityApp
        from tau2.domains.gaia2.vendor.apps.contacts import Contacts, InternalContacts
        from tau2.domains.gaia2.vendor.apps.email_client import EmailClientV2
        from tau2.domains.gaia2.vendor.apps.messaging_v2 import MessagingAppV2
        from tau2.domains.gaia2.vendor.apps.sandbox_file_system import (
            SandboxLocalFileSystem,
        )
        from tau2.domains.gaia2.vendor.apps.shopping import Shopping

        super().__init__(Gaia2DB())
        self.runtime = clock
        self.semantic_scoring = None
        self.source_snapshot = copy.deepcopy(snapshot)
        self.assets = Path(assets)
        self.apps = {}
        self._operations = {}
        self._agent_tools = {}
        classes = {
            c.__name__: c
            for c in [
                RentAFlat,
                CabApp,
                Calendar,
                CityApp,
                Contacts,
                InternalContacts,
                EmailClientV2,
                MessagingAppV2,
                SandboxLocalFileSystem,
                Shopping,
            ]
        }
        for entry in snapshot:
            if entry["name"] == "AgentUserInterface":
                continue
            cls = classes.get(entry["class_name"])
            if cls is None:
                raise ValueError(
                    f"Unsupported Gaia2 application class {entry['class_name']}"
                )
            if cls is SandboxLocalFileSystem:
                app = cls(
                    name=entry["name"],
                    state_directory=str(self.assets / "demo_filesystem"),
                )
            else:
                app = cls()
                app.name = entry["name"]
            # Upstream uses Python's process-randomized hash(). Pin it instead.
            app.seed = int.from_bytes(
                hashlib.sha256(f"{seed}:{app.name}".encode()).digest()[:8], "big"
            )
            app.rng = random.Random(app.seed)
            if clock is not None:
                app.register_time_manager(clock)
            app.load_state(copy.deepcopy(entry["app_state"]))
            self.apps[app.name] = app
        protocols = (
            {Protocol.FILE_SYSTEM: self.apps["Files"]} if "Files" in self.apps else {}
        )
        for app in self.apps.values():
            app.connect_to_protocols(protocols)
            for tool in app.get_tools() + app.get_env_tools() + app.get_data_tools():
                name = tool._public_name or tool.name
                # An application can expose the same callable in several roles.
                self._operations.setdefault(name, tool)
            for tool in app.get_tools():
                name = tool._public_name or tool.name
                self._agent_tools[name] = self._wrap(name, tool)
        self._agent_tools.update(
            {
                f"System__{name}": getattr(self, name)
                for name in [
                    "get_current_time",
                    "get_notifications",
                    "wait_for_notification",
                ]
            }
        )
        self.refresh_db()

    @staticmethod
    def replay_result_equal(name, actual, recorded):
        """Compare set-valued participants without changing conversation/message order.

        Upstream ConversationV2 stores participant_ids as a set; its serialization
        order varies across Python processes. Membership and every other value
        must still agree exactly.
        """
        if name != "Messages__list_recent_conversations":
            return actual == recorded

        def normalize(value):
            if isinstance(value, dict):
                return {
                    k: sorted(v)
                    if k == "participant_ids" and isinstance(v, list)
                    else normalize(v)
                    for k, v in value.items()
                }
            if isinstance(value, list):
                return [normalize(v) for v in value]
            return value

        return normalize(actual) == normalize(recorded)

    def get_user_asr_materials(self, task):
        """Expose initial personal entity names to the private listener only."""
        from tau2.domains.gaia2.asr_materials import user_asr_materials

        return user_asr_materials(self.source_snapshot)

    def _wrap(self, name, tool):
        def call(**arguments):
            return self.execute(name, arguments)

        call.__name__ = name
        call.__doc__ = tool._public_description or tool.function_description or name
        call.__signature__ = inspect.Signature(
            [
                inspect.Parameter(
                    a.name,
                    inspect.Parameter.KEYWORD_ONLY,
                    default=a.default if a.has_default else inspect.Parameter.empty,
                    annotation=a.type_obj or Any,
                )
                for a in tool.args
            ],
            return_annotation=Any,
        )
        kind = ToolType.WRITE if tool.write_operation else ToolType.READ
        return is_tool(kind, mutates_state=True)(call)

    @property
    def tools(self):
        """Agent-visible operations, excluding private environment and scoring hooks."""
        return self._agent_tools

    def get_tools(self, include=None):
        """Expose application and clock tools using ordinary tau2 tool schemas."""
        selected = (
            self.tools
            if include is None
            else {name: self.tools[name] for name in include}
        )
        result = {}
        for name, function in selected.items():
            if function.__name__ != name:

                def wrapped(_function=function, **arguments):
                    return _function(**arguments)

                wrapped.__name__ = name
                wrapped.__doc__ = function.__doc__
                wrapped.__signature__ = inspect.signature(function)
                function = wrapped
            result[name] = Tool(func=function)
        return result

    def execute(self, name: str, arguments: dict):
        """Execute one application operation, including private environment tools."""
        from tau2.domains.gaia2.vendor.application_time import use_clock
        from tau2.domains.gaia2.vendor.identifiers import use_rng

        tool = self._operations.get(name)
        if tool is None:
            raise ValueError(f"Unknown Gaia2 operation {name}")
        with (
            use_rng(tool.class_instance.rng),
            use_clock(self.runtime.time if self.runtime is not None else lambda: 0.0),
        ):
            try:
                return json_value(tool(**arguments))
            finally:
                self.refresh_db()

    def refresh_db(self, *, include_files=False):
        """Synchronize app objects with the serializable tau2 database."""
        self.db.apps = {
            name: json_value(app.get_state())
            for name, app in self.apps.items()
            if name != "Files"
        }
        if include_files and "Files" in self.apps:
            filesystem = self.apps["Files"]
            root = Path(filesystem.tmpdir)
            files = {}
            directories = []
            for path in sorted(root.rglob("*")):
                relative = path.relative_to(root).as_posix()
                if path.is_dir():
                    directories.append(relative)
                    continue
                if not path.is_file():
                    raise ValueError("Unexpected non-file entry in scenario sandbox")
                logical = path
                if "/" + relative in filesystem.local_fs.fallback_stats:
                    logical = self.assets / "demo_filesystem" / relative
                    if not logical.is_file():
                        logical = (
                            path  # A source file without backing content is empty.
                        )
                stat = logical.stat()
                files[relative] = _file_digest(
                    str(logical), stat.st_size, stat.st_mtime_ns
                )
            self.db.apps["Files"] = {"files": files, "directories": directories}

    def close(self):
        """Remove only this toolkit's owned file sandbox."""
        filesystem = self.apps.get("Files")
        if filesystem is not None:
            filesystem.__del__()
            self.apps.pop("Files", None)

    @is_tool(ToolType.READ)
    def get_current_time(self) -> dict:
        """Get the scenario's current UTC date, time and weekday."""
        timestamp = self.runtime.time()
        date = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        return {
            "current_timestamp": timestamp,
            "current_datetime": date.strftime("%Y-%m-%d %H:%M:%S"),
            "current_weekday": date.strftime("%A"),
        }

    @is_tool(ToolType.READ)
    def get_notifications(self) -> list[dict]:
        """Read and consume application change notifications that have arrived."""
        with self.runtime.lock:
            result, self.runtime.notifications = self.runtime.notifications, []
            return result

    @is_tool(ToolType.READ)
    def wait_for_notification(
        self, timeout: int = DEFAULT_SCENARIO_NOTIFICATION_WAIT_SECONDS
    ) -> list[dict]:
        """Wait 0 to 60 seconds for an application update while audio continues.

        Args:
            timeout: Maximum wait in seconds, from 0 to 60. Repeat this tool
                for longer monitoring, checking the authoritative clock.
        """
        if timeout < 0 or timeout > DEFAULT_SCENARIO_NOTIFICATION_WAIT_SECONDS:
            raise ValueError(
                f"timeout must be between 0 and {DEFAULT_SCENARIO_NOTIFICATION_WAIT_SECONDS} seconds; repeat waits for longer monitoring"
            )
        return self.runtime.wait_for_notification(timeout)

    def assert_scenario_goal(self, source_id: str) -> bool:
        """Evaluator-only goal assertion; never present in MCP discovery."""
        from tau2.domains.gaia2.scoring import check_goal

        if source_id != self.runtime.spec.source_id:
            raise ValueError("Scoring contract does not match the running scenario")
        return check_goal(self)

    wait_for_notification.__waits_for_environment__ = True
