"""Where a satellite's TLE comes from.

Every TLE change goes through ``ContactPlanner.set_tle(sat_id, text, source)``,
which checks it, keeps a short history and recomputes contacts. A source only
has to produce TLE text; it never touches contacts or activities itself.

v1 has the manual source (pasted in the tool). To add one (a file drop, a URL
such as CelesTrak, a telemetry packet carrying the TLE), subclass
``TleSource``, implement ``fetch`` and register it in ``SOURCES``. The planner
calls ``fetch`` every ``poll_period`` seconds for each satellite configured
with that source and applies whatever it returns.
"""


class TleSource:
    kind = ""
    label = ""
    poll_period = None  # seconds between fetches; None means the source never polls

    def check_config(self, config):
        """Problems with a satellite's source settings, as messages."""
        return []

    def fetch(self, satellite, config, backend):
        """Return TLE text (two or three lines) if there's one to apply, else None."""
        return None


class ManualSource(TleSource):
    kind = "manual"
    label = "Pasted in the tool"


SOURCES = {source.kind: source for source in (ManualSource(),)}


def get_source(kind):
    source = SOURCES.get(kind or "manual")
    if source is None:
        raise KeyError(f"Unknown TLE source '{kind}' (known: {', '.join(SOURCES)})")
    return source
