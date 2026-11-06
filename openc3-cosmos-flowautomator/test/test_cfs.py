"""realdata/cfs_setup.py: the step 4 pass ops template, against a small fake of cFS behind its gate."""

import os
import sys
import threading
import time

import pytest
from conftest import run_to_end


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "realdata"))

import cfs_setup  # noqa: E402
from flow_automator.graph import validate_flow  # noqa: E402


class FakeCfs:
    """What the real cFS does here: telemetry only after TO_LAB_ENABLE, no-ops count, a restart resets
    everything and stops telemetry until it's switched on again; nothing gets through while the link is down."""

    def __init__(self, backend):
        self.backend = backend
        self.link_up = True
        self.enabled = False
        self.es = 0
        self.sample = 0
        self.ignore = set()  # commands lost once (a dropout)
        self.lag = 0.0  # COSMOS counts a packet before its values reach the current value table
        self.received = 0
        self.stop = threading.Event()
        original = backend.send_cmd

        def send_cmd(command, allow_hazardous=False):
            original(command, allow_hazardous)
            self.command(command.split()[1])

        backend.send_cmd = send_cmd
        self._publish()
        self.thread = threading.Thread(target=self._housekeeping, daemon=True)
        self.thread.start()

    def command(self, name):
        if not self.link_up:
            return
        if name in self.ignore:
            self.ignore.discard(name)
            return
        if name == "TO_LAB_ENABLE":
            self.enabled = True
        elif name == "ES_NOOP":
            self.es += 1
        elif name == "SAMPLE_NOOP":
            self.sample += 1

    def reboot(self):
        self.enabled, self.es, self.sample = False, 0, 0

    def _publish(self, es=None, sample=None, received=None):
        values = self.backend.values
        values["CFS ES_HK CMD_CNT"] = self.es if es is None else es
        values["CFS ES_HK RESET_TYPE"] = "POWERON"
        values["CFS SAMPLE_HK CMD_CNT"] = self.sample if sample is None else sample
        values["CFS ES_HK RECEIVED_COUNT"] = self.received if received is None else received
        values["CFS SAMPLE_HK RECEIVED_COUNT"] = values["CFS ES_HK RECEIVED_COUNT"]

    def _housekeeping(self):
        while not self.stop.is_set():
            if self.link_up and self.enabled:
                self.received += 1
                counts = self.backend.counts
                counts[("CFS", "ES_HK")] = counts.get(("CFS", "ES_HK"), 0) + 1  # counted at once ...
                if self.lag:  # ... its values a moment later
                    snapshot = (self.es, self.sample, self.received)
                    threading.Timer(self.lag, self._publish, snapshot).start()
                else:
                    self._publish()
            time.sleep(0.02)


@pytest.fixture
def cfs(backend):
    fake = FakeCfs(backend)
    yield fake
    fake.stop.set()


@pytest.fixture
def alerts(engine):
    sent = []
    engine.alerts.emit = lambda event, title, message="", **kwargs: sent.append((title, message))
    return sent


def quick():
    flow = cfs_setup.template_flow(acquire_s=0.5, ack_s=0.5, los_quiet_s=0.3)
    flow["template"] = False
    return flow


def contact(seconds=2.0):
    now = time.time()
    return {
        "contact_aos": now,
        "contact_tca": now + seconds / 2,
        "contact_los": now + seconds,
        "contact_satellite": "CFS: NOAA 19",
        "contact_station": "CFS: Fairbanks",
    }


def messages(run):
    return " | ".join(step.get("message") or "" for step in run["steps"])


def test_the_template_is_valid():
    assert validate_flow(cfs_setup.template_flow()) == []


def test_a_nominal_pass(engine, cfs, alerts):
    cfs.link_up = True

    def close_at_los():
        time.sleep(2.0)
        cfs.link_up = False

    threading.Thread(target=close_at_los, daemon=True).start()
    run = run_to_end(engine, quick(), timeout=10, variables=contact(2.0))
    assert cfs_setup.outcome(run) == ("nominal", "ok"), messages(run)
    assert "Pass ops done: ES commands 1, sample 1" in messages(run)
    assert alerts == []


def test_a_lost_acknowledgement_gets_a_second_attempt(engine, cfs, alerts):
    cfs.ignore.add("ES_NOOP")  # the first no-op is lost on the way up
    threading.Thread(target=lambda: (time.sleep(3.0), setattr(cfs, "link_up", False)), daemon=True).start()
    run = run_to_end(engine, quick(), timeout=10, variables=contact(3.0))
    assert cfs_setup.outcome(run)[0] == "recovered", messages(run)
    assert "the first lost its ES no-op acknowledgement" in messages(run)
    assert alerts == []


def test_a_reboot_mid_pass_is_recovered(engine, cfs, alerts):
    original = cfs.command
    rebooted = []

    def reboot_on_sample(name):
        if name == "SAMPLE_NOOP" and not rebooted:
            rebooted.append(True)
            cfs.reboot()  # the flight software restarts as the sample no-op arrives
            return
        original(name)

    cfs.command = reboot_on_sample
    threading.Thread(target=lambda: (time.sleep(3.0), setattr(cfs, "link_up", False)), daemon=True).start()
    run = run_to_end(engine, quick(), timeout=10, variables=contact(3.0))
    assert cfs_setup.outcome(run)[0] == "recovered", messages(run)
    assert "sample app no-op acknowledgement" in messages(run)


def test_a_dead_pass_alerts(engine, cfs, alerts):
    cfs.link_up = False
    run = run_to_end(engine, quick(), timeout=10, variables=contact(1.0))
    assert cfs_setup.outcome(run) == ("failed", "ok"), messages(run)
    assert alerts and alerts[0][0] == "cFS pass ops failed" and "No telemetry in two attempts" in alerts[0][1]


def test_telemetry_after_los_is_reported(engine, cfs, alerts):
    run = run_to_end(engine, quick(), timeout=10, variables=contact(1.0))  # the link never closes
    assert cfs_setup.outcome(run) == ("nominal", "late"), messages(run)
    assert "still arriving" in messages(run)


def test_the_rule_is_accepted_by_the_planner(engine):
    from flow_automator.orbit import make_tle  # noqa: PLC0415
    from test_contacts import EPOCH  # noqa: PLC0415

    planner = engine.planner
    sat = planner.save_satellite({"name": "CFS: NOAA 19", "tle": make_tle(EPOCH, 98.9, 19.8, 3.6, catalog=33591)})
    station = planner.save_station({"name": "CFS: Fairbanks", **cfs_setup.STATION})
    flow, problems = engine.save_flow(cfs_setup.template_flow())
    assert problems == []
    result = planner.save_rule(cfs_setup.rule_payload(flow["id"], sat["id"], station["id"]))
    assert result["rule"]["stop_at_los"] is False and result["rule"]["offset"] == -60


def test_stale_values_after_a_reboot_dont_fool_it(engine, cfs, alerts):
    # Found live (cfs_faults.py reboot scenarios): COSMOS counts a packet before its values are in the current
    # value table. After a reboot the table still held the old command counter, so waiting for "a packet" and
    # reading the counter compared every acknowledgement against the wrong number.
    cfs.es, cfs.sample = 5, 3
    cfs._publish()  # what COSMOS holds from before the reboot
    cfs.reboot()
    cfs.lag = 0.15
    threading.Thread(target=lambda: (time.sleep(3.0), setattr(cfs, "link_up", False)), daemon=True).start()
    flow = cfs_setup.template_flow(acquire_s=1.0, ack_s=1.0, los_quiet_s=0.3)
    flow["template"] = False
    run = run_to_end(engine, flow, timeout=12, variables=contact(3.0))
    assert cfs_setup.outcome(run)[0] == "nominal", messages(run)
    assert "es0 = 0; s0 = 0" in messages(run)  # the counters after the reboot, not the stale 5 and 3
