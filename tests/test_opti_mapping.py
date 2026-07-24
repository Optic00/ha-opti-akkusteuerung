import os
import re
import subprocess

import pytest

from .ha_harness import REPO, FakeHass, find_template_entity, load_yaml, render

SOURCE = "sensor.solcast_pv_forecast_prognose_verbleibende_leistung_heute"

MAPPING_PATH = REPO / "packages" / "opti_mapping.yaml"
EXAMPLE_MAPPING_PATH = REPO / "opti_mapping.example.yaml"
PRIVATE_ENTITY_PATTERN = r"sensor\.sn_[0-9]{6,}"


def _git(*args, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=REPO,
        check=check,
        capture_output=True,
        text=True,
    )


def _pr_history_commits():
    configured_base = os.environ.get("PRIVACY_SCAN_BASE")
    if configured_base:
        base_ref = configured_base
        _git("rev-parse", "--verify", f"{base_ref}^{{commit}}")
    elif _git(
        "rev-parse",
        "--verify",
        "--quiet",
        "refs/remotes/origin/main^{commit}",
        check=False,
    ).returncode == 0:
        base_ref = "refs/remotes/origin/main"
    else:
        # A checkout without the explicit CI base or origin/main cannot identify
        # a PR boundary safely, so scan every commit reachable from HEAD.
        return _git("rev-list", "--reverse", "HEAD").stdout.splitlines()

    merge_base = _git("merge-base", base_ref, "HEAD").stdout.strip()
    assert merge_base, f"Kein Merge-Base fuer Privacy-Scan mit {base_ref!r}"
    return _git(
        "rev-list", "--reverse", f"{merge_base}..HEAD"
    ).stdout.splitlines()


def _commit_serial_violations(commit):
    result = _git(
        "grep",
        "-I",
        "-n",
        "-o",
        "-E",
        PRIVATE_ENTITY_PATTERN,
        commit,
        "--",
        check=False,
    )
    if result.returncode not in (0, 1):
        raise subprocess.CalledProcessError(
            result.returncode,
            result.args,
            output="<redacted>",
            stderr=result.stderr,
        )

    violations = []
    for raw_match in result.stdout.splitlines():
        commit_and_path, line_number, _redacted_match = raw_match.rsplit(":", 2)
        _treeish, relative_path = commit_and_path.split(":", 1)
        violations.append(f"{commit[:12]}:{relative_path}:{line_number}")
    return violations


def _worktree_serial_violations():
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    violations = []
    pattern = re.compile(PRIVATE_ENTITY_PATTERN)

    for raw_path in filter(None, tracked):
        relative_path = raw_path.decode("utf-8")
        content = (REPO / relative_path).read_bytes()
        if b"\0" in content:
            continue
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for _match in pattern.finditer(line):
                violations.append(f"WORKTREE:{relative_path}:{line_number}")
    return violations


def test_pr_historie_und_worktree_enthalten_keine_wr_seriennummern():
    violations = _worktree_serial_violations()
    for commit in _pr_history_commits():
        violations.extend(_commit_serial_violations(commit))

    assert violations == [], (
        f"{len(violations)} private Treffer (Werte redigiert): {violations}"
    )


# packages/opti_mapping.yaml ist bewusst gitignored (private Entitäts-IDs).
# Nur Tests, die diese Datei wirklich lesen, werden in öffentlichen Checkouts
# übersprungen. Der Privacy-Guard oben muss dagegen immer laufen.
requires_private_mapping = pytest.mark.skipif(
    not MAPPING_PATH.exists(),
    reason="privates packages/opti_mapping.yaml nicht vorhanden (gitignored)",
)


def _mapping_cfg():
    return load_yaml(MAPPING_PATH)


def _remaining_today_estimate10(hass):
    cfg = _mapping_cfg()
    entity = find_template_entity(cfg, "sensor", "opti_mapping_forecast_remaining_today_kwh")
    return render(hass, entity["attributes"]["estimate10"])


def _pv_yield_entity():
    return find_template_entity(
        _mapping_cfg(), "sensor", "opti_mapping_pv_yield_today"
    )


def _pv_wr_entities():
    entity = _pv_yield_entity()
    templates = f"{entity['availability']} {entity['state']}"
    entity_ids = sorted(set(
        re.findall(r"sensor\.sn_\d+_daily_yield", templates)
    ))
    assert len(entity_ids) == 2
    return entity_ids


def _grid_import_entity():
    return find_template_entity(
        _mapping_cfg(), "sensor", "opti_mapping_grid_import_w"
    )


def _grid_import_source(entity):
    templates = f"{entity['availability']} {entity['state']}"
    entity_ids = sorted(set(
        re.findall(r"sensor\.sn_\d+_metering_power_absorbed", templates)
    ))
    assert len(entity_ids) == 1
    return entity_ids[0]


def _assert_grid_import_contract(entity, source):
    assert entity["name"] == "Opti Grid Import W"
    assert entity["unit_of_measurement"] == "W"
    assert entity["device_class"] == "power"
    assert entity["state_class"] == "measurement"

    hass = FakeHass(states={source: "321"})
    assert render(hass, entity["availability"]) == "True"
    assert float(render(hass, entity["state"])) == 321

    hass = FakeHass(states={source: "-12"})
    assert float(render(hass, entity["state"])) == 0

    hass = FakeHass(states={source: "unavailable"})
    assert render(hass, entity["availability"]) == "False"


def test_example_mapping_grid_import_contract_is_always_covered():
    entity = find_template_entity(
        load_yaml(EXAMPLE_MAPPING_PATH), "sensor", "opti_mapping_grid_import_w"
    )
    _assert_grid_import_contract(entity, "sensor.DEIN_GRID_IMPORT")


@requires_private_mapping
def test_mapping_remaining_today_reicht_estimate10_durch():
    hass = FakeHass(
        states={SOURCE: "22.26"},
        attrs={SOURCE: {"estimate10": 9.46}},
    )
    assert float(_remaining_today_estimate10(hass)) == 9.46


@requires_private_mapping
def test_mapping_remaining_today_estimate10_fehlt_bleibt_none():
    # Kontrakt (canonical-layer.md): fehlt das P10 an der Quelle, bleibt das
    # Attribut none - NICHT 0, denn 0 waere von "echtes P10 = 0 kWh" nicht
    # unterscheidbar. Seit 2026-07-05 ist die private opti_mapping.yaml auf
    # die none-Variante des Examples angeglichen.
    hass = FakeHass(states={SOURCE: "22.26"})
    assert _remaining_today_estimate10(hass) == "None"


@requires_private_mapping
def test_mapping_pv_ertrag_summiert_beide_wechselrichter():
    entity = _pv_yield_entity()
    pv_wr_1, pv_wr_2 = _pv_wr_entities()
    hass = FakeHass(states={pv_wr_1: "33679", pv_wr_2: "32479"})
    assert float(render(hass, entity["state"])) == 66.158
    assert render(hass, entity["availability"]) == "True"


@requires_private_mapping
def test_mapping_pv_ertrag_braucht_beide_wechselrichter():
    entity = _pv_yield_entity()
    pv_wr_1, pv_wr_2 = _pv_wr_entities()
    hass = FakeHass(states={pv_wr_1: "33679", pv_wr_2: "unavailable"})
    assert render(hass, entity["availability"]) == "False"


@requires_private_mapping
def test_mapping_grid_import_ist_verfuegbar_nichtnegativ_und_hat_power_metadaten():
    entity = _grid_import_entity()
    source = _grid_import_source(entity)
    _assert_grid_import_contract(entity, source)


@requires_private_mapping
def test_taeglicher_pv_aggregat_hat_keine_total_increasing_statistik():
    entity = _pv_yield_entity()
    assert "state_class" not in entity
