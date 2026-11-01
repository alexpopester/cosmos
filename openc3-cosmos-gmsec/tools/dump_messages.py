# Prints GMSEC messages as they arrive so you can see their real shape: every
# field, plus the length and leading bytes of each BIN field. Use it to work
# out the subjects, the field holding the packet bytes, and any header fields
# before configuring the plugin. Runs in the GMSEC image (see README).
#
#   python dump_messages.py mw-id=activemq39 server=tcp://activemq:61616 \
#     subject=GMSEC.> [count=5] [bytes=32]

import sys

import libgmsec_python3 as gmsec


def describe_binary(message, preview):
    iterator = message.get_field_iterator()
    while iterator.has_next():
        field = iterator.next()
        if field.get_type() == gmsec.Field.BIN_TYPE:
            data = bytes(gmsec.to_BinaryField(field).get_value())
            print(
                f"  BIN {field.get_name()}: {len(data)} bytes, first {min(preview, len(data))}: {data[:preview].hex(' ')}"
            )


def main():
    config = gmsec.Config(sys.argv)
    subject = config.get_value("subject", "GMSEC.>")
    count = int(config.get_value("count", "5"))
    preview = int(config.get_value("bytes", "32"))

    conn_mgr = gmsec.ConnectionManager(config)
    conn_mgr.initialize()
    conn_mgr.subscribe(subject)
    print(f"{conn_mgr.get_library_version()}: waiting for {count} message(s) on {subject}", flush=True)
    received = 0
    try:
        while count == 0 or received < count:
            message = conn_mgr.receive(-1)
            if message is None:
                continue
            received += 1
            print(f"\n===== message {received}: {message.get_subject()} =====")
            print(message.to_XML())
            describe_binary(message, preview)
            sys.stdout.flush()
            conn_mgr.release(message)
    except KeyboardInterrupt:
        pass
    finally:
        conn_mgr.cleanup()


if __name__ == "__main__":
    main()
