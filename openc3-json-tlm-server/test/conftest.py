import pathlib
import sys

plugin_root = pathlib.Path(__file__).parent.parent
cosmos_root = plugin_root.parent

# Plugin's python/ dir — resolves json_telemetry_server_interface directly.
python_dir = str(plugin_root / "python")
if python_dir not in sys.path:
    sys.path.insert(0, python_dir)

# openc3 python dir — resolves test.test_helper (mock_redis, setup_system).
openc3_python = str(cosmos_root / "openc3" / "python")
if openc3_python not in sys.path:
    sys.path.insert(0, openc3_python)
