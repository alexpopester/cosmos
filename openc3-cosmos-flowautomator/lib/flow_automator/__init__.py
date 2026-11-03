"""Flow Automator engine: runs node graphs built in the Flow Automator tool.

The engine core (expressions, graph validation, runner, triggers, store, HTTP
API) is plain Python with no COSMOS imports so it can be unit tested on its
own. ``cosmos_backend`` is the only module that talks to COSMOS.
"""
