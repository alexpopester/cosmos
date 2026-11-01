# In-process stand-in for the GMSEC API 4.9.1 Python3 binding, covering only
# the calls GmsecInterface makes. Method names match the SWIG renames in
# GMSEC_API/python3/gmsec4/interfaces/*.i. Tests publish with
# ConnectionManager.bus.put(message).

import queue


class GmsecError(Exception):
    pass


class Field:
    BIN_TYPE = 1
    STRING_TYPE = 9

    def __init__(self, name, value, field_type):
        self._name = name
        self._value = value
        self._type = field_type

    def get_name(self):
        return self._name

    def get_type(self):
        return self._type

    def get_string_value(self):
        if self._type == Field.BIN_TYPE:
            return self._value.hex().upper()
        return str(self._value)


class BinaryField(Field):
    def __init__(self, name, data, length=None):
        super().__init__(name, bytes(data), Field.BIN_TYPE)

    def get_value(self):
        return bytearray(self._value)

    def get_length(self):
        return len(self._value)


def to_BinaryField(field):  # noqa: N802  (matches the binding)
    assert field.get_type() == Field.BIN_TYPE
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
            self._fields[name] = Field(name, value, Field.STRING_TYPE)
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
        if field is None or field.get_type() != Field.BIN_TYPE:
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


class ConnectionManager:
    bus = queue.Queue()
    instances = []

    def __init__(self, config):
        self.config = config
        self.subscribed = []
        self.initialized = False
        self.cleaned_up = False
        self.released = []
        ConnectionManager.instances.append(self)

    @staticmethod
    def get_API_version():  # noqa: N802
        return "GMSEC API v4.9.1 (fake)"

    def get_library_version(self):
        return f"fake {self.config.get_value('mw-id')}"

    def initialize(self):
        if self.config.get_value("mw-id") == "broken":
            raise GmsecError("Unable to connect to middleware")
        self.initialized = True

    def subscribe(self, subject):
        self.subscribed.append(subject)
        return SubscriptionInfo(subject)

    def unsubscribe(self, info):
        self.subscribed.remove(info.subject)

    # Subject filtering is the broker's job, so every queued message is delivered
    def receive(self, timeout_ms=-1):
        if self.cleaned_up:
            raise GmsecError("Connection has been cleaned up")
        try:
            return ConnectionManager.bus.get(timeout=None if timeout_ms < 0 else timeout_ms / 1000)
        except queue.Empty:
            return None

    def release(self, message):
        message.released = True
        self.released.append(message)

    def cleanup(self):
        self.cleaned_up = True
