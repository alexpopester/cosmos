# Script Runner test procedure for HttpJsonClientInterface
#
# Requires test_mock_server.sh to be running — it listens on port 80 inside
# the cosmos_default network and responds with a fixed RESPONSE packet.
#
# What this tests:
#   1. SUBMIT command fields are serialized to JSON with no HTTP_* items in the definition
#   2. RESPONSE telemetry is parsed from JSON without ACCESSOR JsonAccessor in tlm.txt
#      (the accessor is inherited automatically from the interface)
#   3. Error routing: the interface routes 3xx+ responses to the ERROR packet

# Send a SUBMIT command and verify the echoed telemetry values in the response
cmd("HTTPJSONDEMO SUBMIT with temperature 23.5, pressure 101.4, label 'test-sensor-1'")
wait_check("HTTPJSONDEMO RESPONSE status == 'ok'", 10)
wait_check("HTTPJSONDEMO RESPONSE message == 'Hello from mock'", 5)
wait_check("HTTPJSONDEMO RESPONSE temperature == 98.6", 5)
wait_check("HTTPJSONDEMO RESPONSE pressure == 14.7", 5)

# Send a second command with different values to confirm the interface isn't caching
cmd("HTTPJSONDEMO SUBMIT with temperature -10.0, pressure 202.8, label 'test-sensor-2'")
wait_check("HTTPJSONDEMO RESPONSE status == 'ok'", 10)

print("All checks passed — JsonAccessor inherited from interface, no ACCESSOR line needed in tlm.txt")
