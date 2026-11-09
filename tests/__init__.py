"""Test-only AstrBot compatibility stubs."""

import sys
import types


try:
    import astrbot  # type: ignore  # noqa: F401
except ModuleNotFoundError:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")

    class Logger:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: None

    class DummyFilter:
        @staticmethod
        def command(*args, **kwargs):
            return lambda func: func

        @staticmethod
        def llm_tool(*args, **kwargs):
            return lambda func: func

        @staticmethod
        def on_llm_request(*args, **kwargs):
            return lambda func: func

    class AstrMessageEvent:
        pass

    class Context:
        pass

    class Star:
        def __init__(self, context=None):
            self.context = context

    class StarTools:
        @staticmethod
        def get_data_dir(_name):
            return "."

    def register(*args, **kwargs):
        return lambda cls: cls

    api.logger = Logger()
    event.AstrMessageEvent = AstrMessageEvent
    event.filter = DummyFilter()
    star.Context = Context
    star.Star = Star
    star.StarTools = StarTools
    star.register = register
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
        }
    )
