# In-process stand-in for the GMSEC API Python3 binding, covering only the
# calls GmsecInterface makes. Method names match the SWIG renames in
# GMSEC_API/python3/gmsec4/interfaces/*.i (API 4.x) and gmsec5 (API 5.x).
#
# FAKE_GMSEC_API=4 (default) or 5 picks which API this module looks like. Only
# that API's names exist, so code that calls a 4.x-only name fails under 5.
# Tests publish with FakeConnection.bus.put(message) and inspect
# FakeConnection.instances; message construction is a test convenience in both.

import os
import queue


API = int(os.environ.get("FAKE_GMSEC_API", "4"))
_BIN = 1
_STRING = 9


class GmsecError(Exception):
    pass


class Field:
    def __init__(self, name, value, field_type):
        self._name = name
        self._value = value
        self._type = field_type

    def get_name(self):
        return self._name

    def get_type(self):
        return self._type

    def get_string_value(self):
        if self._type == _BIN:
            return self._value.hex().upper()
        return str(self._value)


if API == 4:
    Field.BIN_TYPE = _BIN
    Field.STRING_TYPE = _STRING
else:
    Field.Type_BINARY = _BIN
    Field.Type_STRING = _STRING


class BinaryField(Field):
    def __init__(self, name, data, length=None):
        super().__init__(name, bytes(data), _BIN)

    def get_value(self):
        return bytearray(self._value)

    def get_length(self):
        return len(self._value)


def to_BinaryField(field):  # noqa: N802  (matches the binding)
    assert field.get_type() == _BIN
    return field


class MessageFieldIterator:
    def __init__(self, fields):
        self._fields = list(fields)

    def has_next(self):
        return bool(self._fields)

    def next(self):
        return self._fields.pop(0)


class Message:
    PUBLISH = 1

    def __init__(self, subject, kind=PUBLISH):
        self._subject = subject
        self._fields = {}
        self.released = False

    def add_field(self, name, value, length=None):
        if isinstance(value, (bytes, bytearray)):
            self._fields[name] = BinaryField(name, value, length)
        else:
            self._fields[name] = Field(name, value, _STRING)
        return self

    def get_subject(self):
        return self._subject

    def has_field(self, name):
        return name in self._fields

    def get_field_type(self, name):
        if name not in self._fields:
            raise GmsecError(f"Message does not contain field {name}")
        return self._fields[name].get_type()

    def get_binary_field(self, name):
        field = self._fields.get(name)
        if field is None or field.get_type() != _BIN:
            raise GmsecError(f"Message does not contain BinaryField {name}")
        return field

    def get_string_value(self, name):
        if name not in self._fields:
            raise GmsecError(f"Message does not contain field {name}")
        return self._fields[name].get_string_value()

    def get_field_iterator(self):
        return MessageFieldIterator(self._fields.values())


class Config:
    def __init__(self, *args):
        self.values = {}

    def add_value(self, key, value):
        self.values[key] = value

    def get_value(self, key, default=None):
        return self.values.get(key, default)


class SubscriptionInfo:
    def __init__(self, subject):
        self.subject = subject


class FakeConnection:
    """ConnectionManager in API 4.x, Connection in 5.x."""

    bus = queue.Queue()
    instances = []

    def __init__(self, config):
        self.config = config
        self.subscribed = []
        self.subscribe_configs = {}
        self.started = False
        self.closed = False
        FakeConnection.instances.append(self)

    @staticmethod
    def get_API_version():  # noqa: N802
        return f"GMSEC API v{'4.9.1' if API == 4 else '5.3'} (fake)"

    def get_library_version(self):
        return f"fake {self.config.get_value('mw-id')}"

    def _start(self):
        if self.config.get_value("mw-id") == "broken":
            raise GmsecError("Unable to connect to middleware")
        self.started = True

    def _close(self):
        self.closed = True

    def subscribe(self, subject, config=None):
        self.subscribed.append(subject)
        if config is not None:
            self.subscribe_configs[subject] = config
        return SubscriptionInfo(subject)

    def unsubscribe(self, info):
        self.subscribed.remove(info.subject)

    # Subject filtering is the broker's job, so every queued message is delivered
    def receive(self, timeout_ms=-1):
        if self.closed:
            raise GmsecError("Connection has been closed")
        try:
            return FakeConnection.bus.get(timeout=None if timeout_ms < 0 else timeout_ms / 1000)
        except queue.Empty:
            return None


def _release(message):
    message.released = True


if API == 4:
    FakeConnection.initialize = FakeConnection._start
    FakeConnection.cleanup = FakeConnection._close
    FakeConnection.release = lambda self, message: _release(message)
    ConnectionManager = FakeConnection
else:
    FakeConnection.connect = FakeConnection._start
    FakeConnection.disconnect = FakeConnection._close
    Message.destroy = staticmethod(_release)
    Connection = FakeConnection
