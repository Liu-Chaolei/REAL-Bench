"""Reply-window helpers shared by live and replay runtimes."""


def close_window(row: dict, timestamp: float) -> None:
    """Close a request at an ordinary user turn boundary."""
    row["window_closed_at"] = timestamp
