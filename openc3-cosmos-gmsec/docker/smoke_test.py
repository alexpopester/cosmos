# Build-time smoke test for the GMSEC image, run from the installed bin dir:
# the binding imports under the venv Python, binary fields round trip (the GIL
# fix in the Dockerfile), and the ActiveMQ wrapper loads (no broker needed).
# Works for GMSEC API 4.x and 5.x.
import ctypes

import libgmsec_python3 as gmsec


if hasattr(gmsec, "ConnectionManager"):
    print(gmsec.ConnectionManager.get_API_version())
    message = gmsec.Message("A.B", gmsec.Message.PUBLISH)
    message.add_field("DATA", bytearray(b"\x01\x02\x03"), 3)
    bin_type = gmsec.Field.BIN_TYPE
else:
    print(gmsec.Connection.get_api_version())
    message = gmsec.Message()
    message.set_subject("A.B")
    message.add_field(gmsec.BinaryField("DATA", bytearray(b"\x01\x02\x03"), 3))
    bin_type = gmsec.Field.Type_BINARY

assert bytes(message.get_binary_field("DATA").get_value()) == b"\x01\x02\x03"
assert message.get_field_type("DATA") == bin_type
print("binary fields ok")
ctypes.CDLL("./libgmsec_activemq395.so")
print("activemq395 wrapper loads")
