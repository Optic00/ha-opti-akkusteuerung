"""Jinja-Syntaxpruefung fuer ALLE aktiven Packages und Automationen.

Bisher hatten nur einzelne Pakete so einen Test (byd_*, opti_ki_analyse,
opti_mapping.example). Ausgerechnet opti_derived.yaml und opti_strategie.yaml -
die groessten Template-Traeger - waren nicht abgedeckt: ein Syntaxfehler dort
faellt sonst erst beim Reload in HA auf.

Bewusst rekursiv ueber die geladene YAML-Struktur statt per Regex: eine
Regex-Suche nach Template-Strings hat in diesem Repo schon Falsch-Positive
produziert (Templates stehen in Attributen, verschachtelten choose-Zweigen und
Listenelementen). legacy/ und old/ sind Archive und bleiben aussen vor.
"""
from __future__ import annotations

import jinja2
import pytest

from .ha_harness import REPO, load_yaml

VERZEICHNISSE = ["packages", "automations"]
EXTRA = ["opti_mapping.example.yaml"]


def _dateien():
    gefunden = []
    for verzeichnis in VERZEICHNISSE:
        gefunden += sorted((REPO / verzeichnis).glob("*.yaml"))
    gefunden += [REPO / name for name in EXTRA]
    return gefunden


def _walk_templates(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk_templates(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_templates(v, f"{path}[{i}]")
    elif isinstance(node, str) and ("{{" in node or "{%" in node):
        yield path, node


def test_dateien_gefunden():
    # Schutz gegen einen still leerlaufenden Test (z.B. nach Umbenennung).
    namen = {p.name for p in _dateien()}
    assert "opti_derived.yaml" in namen
    assert "opti_strategie.yaml" in namen
    assert len(namen) >= 8


@pytest.mark.parametrize("pfad", _dateien(), ids=lambda p: p.name)
def test_jinja_parst(pfad):
    cfg = load_yaml(pfad)
    env = jinja2.Environment()
    fehler = []
    for stelle, template in _walk_templates(cfg):
        try:
            env.parse(template)
        except jinja2.TemplateSyntaxError as exc:
            fehler.append(f"{stelle}: {exc}")
    assert fehler == []


# Untergrenzen sind Struktur-Wachhunde, keine exakten Zaehlwerte: sie fallen,
# wenn der Walk an einer Umstrukturierung vorbeiläuft, nicht bei jedem Edit.
# Ist-Stand 25.07.2026: derived 82, strategie 24, ki_analyse 34+14 (zwei Dateien
# gleichen Namens in packages/ und automations/, hier zusammengezaehlt).
@pytest.mark.parametrize("name,minimum", [
    ("opti_derived.yaml", 70),
    ("opti_strategie.yaml", 20),
    ("opti_ki_analyse.yaml", 40),
])
def test_walk_findet_die_templates(name, minimum):
    """Gegenprobe zum Parse-Test: findet der Walk in den Template-Traegern zu
    wenig, prueft der Test oben ins Leere. Nicht jede Datei traegt Templates
    (sma_modbus/sma_statistik/sma_helpers sind reine Definitionen), deshalb
    werden hier nur die grossen Traeger festgenagelt."""
    treffer = []
    for pfad in _dateien():
        if pfad.name == name:
            treffer += list(_walk_templates(load_yaml(pfad)))
    assert len(treffer) >= minimum
