"""CelesTrak and Space-Track TLE sources (tle_sources.py), against fake services."""

import json

import pytest
from flow_automator.backup import make_backup
from flow_automator.contacts import PlannerError
from flow_automator.http_api import FlowApi
from flow_automator.tle_sources import FIRST_BACKOFF_SECONDS, TleSourceError, catalog_key
from test_contacts import EPOCH, ISS_1, ISS_2, with_checksum
from test_triggers import fake_authorize


HOUR = 3600
# The same orbit a little later: newer epoch, small enough move to apply
ISS_1_NEWER = with_checksum("1 25544U 98067A   24001.50100000  .00016717  00000-0  10270-3 0  900")
OTHER_1 = with_checksum("1 43013U 17073A   24001.50000000  .00000044  00000-0  41372-4 0  9990")
OTHER_2 = with_checksum("2 43013  98.7184 340.2050 0001226  91.4651 268.6668 14.19554458315330")


class Response:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text
        self.content = text.encode()


class FakeService:
    """Answers like CelesTrak and Space-Track; records every request."""

    def __init__(self):
        self.calls = []
        self.celestrak = {"25544": f"ISS (ZARYA)\n{ISS_1}\n{ISS_2}\n"}
        self.celestrak_status = 200
        self.spacetrack = f"0 ISS (ZARYA)\n{ISS_1}\n{ISS_2}\n0 OTHER\n{OTHER_1}\n{OTHER_2}\n"
        self.login_text = '""'

    def __call__(self):
        return self  # used as the session factory

    def get(self, url, params=None, **kwargs):
        assert kwargs.get("allow_redirects") is False
        self.calls.append(("GET", url, params))
        if "gp.php" in url:
            if self.celestrak_status != 200:
                return Response(self.celestrak_status, "Forbidden")
            return Response(200, self.celestrak.get(str(params["CATNR"]), "No GP data found"))
        if "/basicspacedata/" in url:
            return Response(200, self.spacetrack)
        return Response(200, "")

    def post(self, url, data=None, **kwargs):
        self.calls.append(("POST", url, data))
        return Response(200, self.login_text)

    def close(self):
        pass


@pytest.fixture
def service(engine):
    fake = FakeService()
    engine.planner.fetcher.session_factory = fake
    engine.planner.clock = lambda: EPOCH
    return fake


def add_sat(planner, kind, catalog="25544", name="ISS", tle=None):
    payload = {"name": name, "catalog": catalog, "tle_source": {"kind": kind}}
    if tle:
        payload["tle"] = tle
    return planner.save_satellite(payload)


def test_celestrak_fills_in_a_new_satellite_and_waits_its_hours(engine, service):
    planner = engine.planner
    sat = add_sat(planner, "celestrak")
    planner.fetcher.poll(EPOCH)
    assert service.calls == [
        ("GET", "https://celestrak.org/NORAD/elements/gp.php", {"CATNR": "25544", "FORMAT": "TLE"})
    ]
    assert planner.satellites[sat["id"]]["tle"]["line1"] == ISS_1
    assert planner.satellites[sat["id"]]["tle"]["source"] == "celestrak"
    assert planner.fetcher.sat_status[sat["id"]]["result"] == "applied"
    planner.fetcher.poll(EPOCH + 3 * HOUR)
    assert len(service.calls) == 1  # every 4 hours by default
    planner.fetcher.poll(EPOCH + 4 * HOUR)
    assert len(service.calls) == 2
    assert planner.fetcher.sat_status[sat["id"]]["result"] == "unchanged"


def test_fetched_tles_go_through_the_safeguards(engine, service):
    planner = engine.planner
    sat = add_sat(planner, "celestrak", tle=f"{ISS_1_NEWER}\n{ISS_2}")
    planner.fetcher.poll(EPOCH)
    status = planner.fetcher.sat_status[sat["id"]]
    assert status["result"] == "skipped" and "newer" in status["reason"]  # CelesTrak's is older
    assert planner.satellites[sat["id"]]["tle"]["line1"] == ISS_1_NEWER


def test_fetch_now_respects_the_minimum_interval(engine, service):
    planner = engine.planner
    add_sat(planner, "celestrak")
    planner.fetcher.poll(EPOCH)
    result = planner.fetcher.fetch_now("celestrak")
    assert result["celestrak"]["now"] == 0 and result["celestrak"]["waiting_until"] == {"25544": EPOCH + 2 * HOUR}
    planner.fetcher.poll(EPOCH + 1 * HOUR)
    assert len(service.calls) == 1
    planner.fetcher.fetch_now("celestrak")
    planner.fetcher.poll(EPOCH + 2 * HOUR)  # forced: the 2 h minimum, not the 4 h schedule
    assert len(service.calls) == 2


def test_a_refused_source_backs_off_and_doubles(engine, service):
    planner = engine.planner
    add_sat(planner, "celestrak")
    service.celestrak_status = 403
    planner.fetcher.poll(EPOCH)
    status = planner.fetcher.status["celestrak"]
    assert "403" in status["error"] and status["retry_at"] == EPOCH + FIRST_BACKOFF_SECONDS
    planner.fetcher.poll(EPOCH + 60)
    assert len(service.calls) == 1
    planner.fetcher.poll(EPOCH + FIRST_BACKOFF_SECONDS)
    assert len(service.calls) == 2
    assert planner.fetcher.status["celestrak"]["retry_at"] == EPOCH + 3 * FIRST_BACKOFF_SECONDS  # doubled
    service.celestrak_status = 200
    planner.fetcher.poll(EPOCH + 3 * FIRST_BACKOFF_SECONDS)
    assert planner.fetcher.status["celestrak"]["error"] is None


def test_unknown_catalog_is_reported_per_satellite(engine, service):
    planner = engine.planner
    sat = add_sat(planner, "celestrak", catalog="99999", name="Gone")
    planner.fetcher.poll(EPOCH)
    status = planner.fetcher.sat_status[sat["id"]]
    assert status["result"] == "missing" and "no element set" in status["reason"]
    assert planner.fetcher.status["celestrak"]["error"] is None  # the source itself worked


def test_spacetrack_needs_its_secrets_and_never_shows_them(engine, service):
    planner = engine.planner
    add_sat(planner, "spacetrack")
    planner.fetcher.poll(EPOCH)
    assert service.calls == []
    assert "SPACETRACK_USERNAME" in planner.fetcher.status["spacetrack"]["error"]
    assert planner.fetcher.settings_json()["spacetrack"]["credentials_set"] is False

    engine.backend.secrets = {"SPACETRACK_USERNAME": "ops@example.com", "SPACETRACK_PASSWORD": "hunter2"}
    assert planner.fetcher.settings_json()["spacetrack"]["credentials_set"] is True
    shown = json.dumps(planner.fetcher.settings_json()) + json.dumps(make_backup(engine))
    assert "hunter2" not in shown and "ops@example.com" not in shown


def test_spacetrack_logs_in_queries_every_catalog_at_once_and_logs_out(engine, service):
    planner = engine.planner
    engine.backend.secrets = {"SPACETRACK_USERNAME": "ops", "SPACETRACK_PASSWORD": "pw"}
    iss = add_sat(planner, "spacetrack")
    other = add_sat(planner, "spacetrack", catalog="43013", name="Other")
    gone = add_sat(planner, "spacetrack", catalog="00005", name="Vanguard")
    planner.fetcher.poll(EPOCH)
    methods = [(m, url.rsplit("/", 2)[-2] if "ajaxauth" in url else url) for m, url, _ in service.calls]
    assert methods[0] == ("POST", "ajaxauth") and service.calls[0][2] == {"identity": "ops", "password": "pw"}
    assert service.calls[1][1].endswith("/class/gp/NORAD_CAT_ID/25544,43013,5/orderby/NORAD_CAT_ID/format/3le")
    assert service.calls[-1][1].endswith("/ajaxauth/logout")
    assert planner.satellites[iss["id"]]["tle"]["source"] == "spacetrack"
    assert planner.satellites[other["id"]]["tle"]["line1"] == OTHER_1
    assert planner.fetcher.sat_status[gone["id"]]["result"] == "missing"


def test_spacetrack_login_failure_backs_off(engine, service):
    planner = engine.planner
    engine.backend.secrets = {"SPACETRACK_USERNAME": "ops", "SPACETRACK_PASSWORD": "wrong"}
    add_sat(planner, "spacetrack")
    service.login_text = '{"Login":"Failed"}'
    planner.fetcher.poll(EPOCH)
    assert "login failed" in planner.fetcher.status["spacetrack"]["error"]
    assert len(service.calls) == 1  # no query after a failed login


def test_internet_sources_need_a_catalog_number(engine):
    with pytest.raises(PlannerError, match="catalog number"):
        engine.planner.save_satellite({"name": "Nameless", "tle_source": {"kind": "celestrak"}})
    sat = engine.planner.save_satellite(
        {"name": "Pasted", "tle_source": {"kind": "celestrak"}, "tle": f"{ISS_1}\n{ISS_2}"}
    )
    assert sat["catalog"] == 25544  # taken from the pasted TLE


def test_settings_are_checked(engine):
    fetcher = engine.planner.fetcher
    with pytest.raises(TleSourceError, match="every 2 to 168"):
        fetcher.save_settings({"celestrak": {"hours": 1}})
    with pytest.raises(TleSourceError, match="link-local"):
        fetcher.save_settings({"celestrak": {"url": "http://169.254.169.254"}})
    with pytest.raises(TleSourceError, match="https"):
        fetcher.save_settings({"celestrak": {"url": "ftp://example.com"}})
    saved = fetcher.save_settings({"celestrak": {"url": "https://mirror.example.com/", "hours": 6}})
    assert saved["celestrak"]["url"] == "https://mirror.example.com" and saved["celestrak"]["hours"] == 6
    assert engine.store.records("planner_settings")["tle_sources"]["celestrak"]["hours"] == 6


def test_a_mirror_that_redirects_is_an_error(engine, service):
    planner = engine.planner
    add_sat(planner, "celestrak")
    service.celestrak_status = 301
    planner.fetcher.poll(EPOCH)
    assert "301" in planner.fetcher.status["celestrak"]["error"]


def test_tle_sources_api(engine, service):
    api = FlowApi(engine, fake_authorize)
    add_sat(engine.planner, "celestrak")
    auth = {"Authorization": "good"}
    status, settings = api.handle("GET", "/flow-automator/api/tle_sources", {}, auth, "")
    assert status == 200 and settings["celestrak"]["satellites"] == 1 and settings["celestrak"]["min_hours"] == 2
    status, _ = api.handle("PUT", "/flow-automator/api/tle_sources", {}, auth, json.dumps({"celestrak": {"hours": 1}}))
    assert status == 400
    status, result = api.handle(
        "POST", "/flow-automator/api/tle_sources/fetch", {}, auth, json.dumps({"kind": "celestrak"})
    )
    assert status == 200 and result["celestrak"]["now"] == 1
    status, _ = api.handle("POST", "/flow-automator/api/tle_sources/fetch", {}, auth, json.dumps({"kind": "manual"}))
    assert status == 400
    assert engine.audit.entries(kind="tle sources")[0]["action"] == "fetched TLEs"


def test_catalog_numbers_compare_without_leading_zeros():
    assert catalog_key("05544") == catalog_key(5544) == "5544"
    assert catalog_key(" a0001 ") == "A0001"


def test_restoring_a_backup_keeps_tle_source_settings(engine):
    engine.planner.fetcher.save_settings({"spacetrack": {"hours": 8, "username_secret": "SPACETRACK_OPS_USER"}})
    backup = make_backup(engine)
    engine.planner.fetcher.save_settings({"spacetrack": {"hours": 2, "username_secret": "SPACETRACK_USERNAME"}})
    from flow_automator.backup import restore_backup

    result = restore_backup(engine, backup)
    assert "TLE sources" in result["settings"]
    assert engine.planner.fetcher.settings["spacetrack"]["username_secret"] == "SPACETRACK_OPS_USER"


# B8 (S-1, C-5, R-3, R-13): the Space-Track login used to go wherever a write user pointed it
@pytest.mark.parametrize(
    "url", ["http://attacker.invalid", "https://attacker.example.com", "http://www.space-track.org"]
)
def test_spacetrack_address_cannot_be_changed(engine, url):
    with pytest.raises(TleSourceError, match="can't be changed"):
        engine.planner.fetcher.save_settings({"spacetrack": {"url": url}})


@pytest.mark.parametrize("name", ["OTHER_DB_PASSWORD", "AWS_SECRET", "spacetrack_user"])
def test_spacetrack_secret_names_must_be_spacetracks(engine, name):
    with pytest.raises(TleSourceError, match="must start with SPACETRACK_"):
        engine.planner.fetcher.save_settings({"spacetrack": {"password_secret": name}})


def test_stored_spacetrack_settings_cannot_redirect_the_login(engine, service):
    # As a restored backup or an older version could have left them
    engine.store.save_record(
        "planner_settings",
        "tle_sources",
        {"spacetrack": {"url": "http://attacker.invalid", "password_secret": "OTHER_DB_PASSWORD", "hours": 4}},
    )
    fetcher = engine.planner.fetcher
    fetcher.load()
    assert fetcher.settings["spacetrack"]["url"] == "https://www.space-track.org"
    assert fetcher.settings["spacetrack"]["password_secret"] == "SPACETRACK_PASSWORD"


def test_spacetrack_login_only_goes_to_space_track(engine, service):
    from flow_automator.tle_sources import SpacetrackSource

    engine.backend.secrets = {"SPACETRACK_USERNAME": "u", "SPACETRACK_PASSWORD": "p", "OTHER": "s3cr3t"}
    settings = {"url": "http://attacker.invalid", "username_secret": "SPACETRACK_USERNAME", "password_secret": "OTHER"}
    with pytest.raises(Exception, match="SPACETRACK_"):
        SpacetrackSource().fetch(["25544"], settings, engine.backend, service)
    assert service.calls == []
    settings["password_secret"] = "SPACETRACK_PASSWORD"
    SpacetrackSource().fetch(["25544"], settings, engine.backend, service)
    urls = [url for _m, url, _d in service.calls]
    assert urls and all(url.startswith("https://www.space-track.org/") for url in urls), urls


@pytest.mark.parametrize("catalog", [">0/limit/100000", "25544,1", "I0001", "12345678901"])
def test_catalog_numbers_are_checked(engine, catalog):
    from flow_automator.contacts import PlannerError

    with pytest.raises(PlannerError, match="Catalog number"):
        engine.planner.save_satellite({"name": "Sat", "catalog": catalog})


def test_alpha5_catalogs_are_queried_by_number():
    from flow_automator.tle_sources import catalog_number, valid_catalog

    assert valid_catalog("A0001") and valid_catalog("25544") and not valid_catalog("O0001")
    assert catalog_number("A0001") == 100001
    assert catalog_number("Z9999") == 339999
    assert catalog_number("05544") == 5544


# B17 (C-10, R-12, DOC-9): rate limits that didn't survive restarts, refusals or a failure mid-batch
def test_rate_limit_survives_restart(engine, service):
    from flow_automator.tle_sources import TleFetcher

    planner = engine.planner
    add_sat(planner, "celestrak")
    planner.fetcher.poll(EPOCH)
    assert len(service.calls) == 1
    restarted = TleFetcher(planner, session_factory=service)
    restarted.load()
    restarted.poll(EPOCH + 60)
    assert len(service.calls) == 1  # still fetched an hour ago as far as the new engine knows


def test_fetch_now_waits_out_a_refusal(engine, service):
    planner = engine.planner
    add_sat(planner, "celestrak")
    service.celestrak_status = 403
    planner.fetcher.poll(EPOCH)
    assert len(service.calls) == 1 and planner.fetcher.status["celestrak"]["refused"] is True
    for _ in range(5):
        result = planner.fetcher.fetch_now("celestrak")
        planner.fetcher.poll(EPOCH + 60)
    assert len(service.calls) == 1
    assert result["celestrak"]["refused_until"] > EPOCH + 60


def test_a_failure_part_way_keeps_what_was_fetched(engine, service):
    planner = engine.planner
    service.celestrak["43013"] = f"OTHER\n{OTHER_1}\n{OTHER_2}\n"
    add_sat(planner, "celestrak")
    add_sat(planner, "celestrak", catalog="43013", name="OTHER")
    real_get = service.get

    def get(url, params=None, **kwargs):
        if params and params.get("CATNR") == "43013":
            raise requests.ConnectionError("reset")
        return real_get(url, params, **kwargs)

    import requests

    service.get = get
    planner.fetcher.poll(EPOCH)
    fetched = planner.fetcher.fetched
    assert ("celestrak", "25544") in fetched and ("celestrak", "43013") not in fetched
    assert planner.fetcher.status["celestrak"]["error"]


def test_a_failing_source_alerts_once(engine, service):
    sent = []
    engine.alerts.emit = lambda event, *a, **k: sent.append(event)
    planner = engine.planner
    add_sat(planner, "celestrak")
    service.celestrak_status = 500
    planner.fetcher.poll(EPOCH)
    planner.fetcher.poll(EPOCH + 3600)
    assert sent.count("tle_source_failing") == 1
