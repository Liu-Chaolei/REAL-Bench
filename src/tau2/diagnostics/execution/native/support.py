"""Small transport-free compatibility surface for published checker functions."""

from jinja2 import Environment, StrictUndefined


def jinja_format(template, skip_validation=False, **kwargs):
    return Environment(undefined=StrictUndefined).from_string(template).render(**kwargs)


def injected_traceable(**kwargs):
    return lambda fn: fn


class ToolJudge:
    def __init__(self, config, kind):
        self.config = config
        self.tool_name = config.tool_name
