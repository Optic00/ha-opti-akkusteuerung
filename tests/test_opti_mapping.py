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


def _resolved_commit(ref):
    result = _git(
        "rev-parse",
        "--verify",
        "--quiet",
        f"{ref}^{{commit}}",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("Privacy scan rejected an invalid explicit base")
    return result.stdout.strip()


def _merge_base(left, right):
    result = _git("merge-base", left, right, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(
            "Privacy scan could not establish a complete base-to-head range"
        )
    return result.stdout.strip()


def _pr_history_commits():
    if _git("rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        raise RuntimeError(
            "Privacy scan requires a non-shallow checkout; configure "
            "fetch-depth: 0 and fetch full history"
        )

    configured_value = os.environ.get("PRIVACY_SCAN_BASE")
    has_explicit_base = configured_value is not None
    configured_base = configured_value.strip() if configured_value else ""
    if has_explicit_base and not configured_base:
        raise RuntimeError("Privacy scan rejected an invalid explicit base")

    origin_main_available = _git(
        "rev-parse",
        "--verify",
        "--quiet",
        "refs/remotes/origin/main^{commit}",
        check=False,
    ).returncode == 0
    head = _resolved_commit("HEAD")

    if has_explicit_base:
        explicit_commit = _resolved_commit(configured_base)
        merge_base = _merge_base(explicit_commit, head)
        commits = _git(
            "rev-list", "--reverse", f"{merge_base}..{head}"
        ).stdout.splitlines()
        if not commits:
            raise RuntimeError(
                "Privacy scan rejected explicit base because it creates an "
                "empty range"
            )

        if origin_main_available:
            origin_merge_base = _merge_base("refs/remotes/origin/main", head)
            explicit_is_same_or_older = _git(
                "merge-base",
                "--is-ancestor",
                merge_base,
                origin_merge_base,
                check=False,
            )
            if explicit_is_same_or_older.returncode not in (0, 1):
                raise RuntimeError(
                    "Privacy scan could not validate the explicit base"
                )
            if explicit_is_same_or_older.returncode == 1:
                raise RuntimeError(
                    "Privacy scan rejected explicit base because it would "
                    "truncate the origin/main range"
                )
            return commits

        # Without origin/main there is no trusted boundary against which the
        # explicit base can be checked. Scan all reachable history instead of
        # risking a silently truncated range.
        return _git("rev-list", "--reverse", head).stdout.splitlines()

    if not origin_main_available:
        # A checkout without the explicit CI base or origin/main cannot identify
        # a PR boundary safely, so scan every commit reachable from HEAD.
        return _git("rev-list", "--reverse", head).stdout.splitlines()

    merge_base = _merge_base("refs/remotes/origin/main", head)
    return _git(
        "rev-list", "--reverse", f"{merge_base}..{head}"
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
            stderr="<redacted>",
        )

    violations = []
    for raw_match in result.stdout.splitlines():
        commit_and_path, line_number, _redacted_match = raw_match.rsplit(":", 2)
        _treeish, relative_path = commit_and_path.split(":", 1)
        violations.append(f"{commit[:12]}:{relative_path}:{line_number}")

    message = _git("show", "-s", "--format=%B", commit, check=False)
    if message.returncode != 0:
        raise subprocess.CalledProcessError(
            message.returncode,
            message.args,
            output="<redacted>",
            stderr="<redacted>",
        )
    if re.search(PRIVATE_ENTITY_PATTERN, message.stdout):
        violations.append(f"{commit[:12]}:COMMIT_MESSAGE")

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


def _test_git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _new_history_test_repo(tmp_path):
    repo = tmp_path / "history-repo"
    repo.mkdir()
    _test_git(repo, "init", "-b", "main")
    _test_git(repo, "config", "user.name", "Privacy Test")
    _test_git(repo, "config", "user.email", "privacy-test")
    return repo


def _history_test_commit(repo, marker):
    (repo / "history.txt").write_text(f"{marker}\n", encoding="utf-8")
    _test_git(repo, "add", "history.txt")
    _test_git(repo, "commit", "-m", marker)
    return _test_git(repo, "rev-parse", "HEAD")


def _use_history_test_repo(monkeypatch, repo):
    monkeypatch.setitem(_git.__globals__, "REPO", repo)


def test_history_scan_lehnt_shallow_checkout_sanitized_ab(tmp_path, monkeypatch):
    source = _new_history_test_repo(tmp_path)
    _history_test_commit(source, "base")
    _history_test_commit(source, "feature")
    shallow = tmp_path / "shallow"
    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            "main",
            source.resolve().as_uri(),
            str(shallow),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert _test_git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    _use_history_test_repo(monkeypatch, shallow)
    monkeypatch.delenv("PRIVACY_SCAN_BASE", raising=False)

    with pytest.raises(RuntimeError, match=r"fetch-depth: 0.*full history"):
        _pr_history_commits()


def test_history_scan_lehnt_explizites_head_als_leere_range_ab(
    tmp_path, monkeypatch
):
    repo = _new_history_test_repo(tmp_path)
    base = _history_test_commit(repo, "base")
    _test_git(repo, "update-ref", "refs/remotes/origin/main", base)
    _history_test_commit(repo, "feature")
    _use_history_test_repo(monkeypatch, repo)
    monkeypatch.setenv("PRIVACY_SCAN_BASE", "HEAD")

    with pytest.raises(RuntimeError, match=r"explicit base.*empty range"):
        _pr_history_commits()


def test_history_scan_lehnt_spaetere_base_als_origin_main_ab(
    tmp_path, monkeypatch
):
    repo = _new_history_test_repo(tmp_path)
    origin_base = _history_test_commit(repo, "base")
    _test_git(repo, "update-ref", "refs/remotes/origin/main", origin_base)
    later_base = _history_test_commit(repo, "feature-one")
    _history_test_commit(repo, "feature-two")
    _use_history_test_repo(monkeypatch, repo)
    monkeypatch.setenv("PRIVACY_SCAN_BASE", later_base)

    with pytest.raises(RuntimeError, match=r"explicit base.*truncate"):
        _pr_history_commits()


def test_history_scan_ohne_origin_main_scannt_trotz_spaeterer_base_alle_commits(
    tmp_path, monkeypatch
):
    repo = _new_history_test_repo(tmp_path)
    _history_test_commit(repo, "base")
    later_base = _history_test_commit(repo, "feature-one")
    _history_test_commit(repo, "feature-two")
    all_commits = _test_git(
        repo, "rev-list", "--reverse", "HEAD"
    ).splitlines()
    _use_history_test_repo(monkeypatch, repo)
    monkeypatch.setenv("PRIVACY_SCAN_BASE", later_base)

    assert _pr_history_commits() == all_commits


def test_history_scan_redigiert_treffer_im_commit_text(tmp_path, monkeypatch):
    repo = _new_history_test_repo(tmp_path)
    base = _history_test_commit(repo, "base")
    _test_git(repo, "update-ref", "refs/remotes/origin/main", base)
    protected_value = "".join(("sensor", ".", "sn_", "123456"))
    (repo / "history.txt").write_text("safe feature\n", encoding="utf-8")
    _test_git(repo, "add", "history.txt")
    _test_git(repo, "commit", "-m", protected_value)
    protected_commit = _test_git(repo, "rev-parse", "HEAD")
    _use_history_test_repo(monkeypatch, repo)
    monkeypatch.delenv("PRIVACY_SCAN_BASE", raising=False)

    violations = []
    for commit in _pr_history_commits():
        violations.extend(_commit_serial_violations(commit))

    assert violations == [f"{protected_commit[:12]}:COMMIT_MESSAGE"]
    assert protected_value not in repr(violations)


def test_history_scan_erlaubt_auto_leere_range_auf_full_history_main(
    tmp_path, monkeypatch
):
    repo = _new_history_test_repo(tmp_path)
    main_head = _history_test_commit(repo, "base")
    _test_git(repo, "update-ref", "refs/remotes/origin/main", main_head)
    _use_history_test_repo(monkeypatch, repo)
    monkeypatch.delenv("PRIVACY_SCAN_BASE", raising=False)

    assert _test_git(repo, "rev-parse", "--is-shallow-repository") == "false"
    assert _pr_history_commits() == []


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

    hass = FakeHass(states={source: "0"})
    assert render(hass, entity["availability"]) == "True"
    assert float(render(hass, entity["state"])) == 0

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
