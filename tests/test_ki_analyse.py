"""Tests fuer das KI-Analyse-Paket (Phase 1)."""
import json
import pathlib

import jinja2
import yaml

from .ha_harness import REPO, FakeHass, load_yaml, render

KI_PACKAGE = REPO / "packages" / "opti_ki_analyse.yaml"
KI_AUTOMATIONS = REPO / "automations" / "opti_ki_analyse.yaml"


def _walk_templates(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk_templates(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_templates(v, f"{path}[{i}]")
    elif isinstance(node, str) and ("{{" in node or "{%" in node):
        yield path, node


def test_ki_package_jinja_parst():
    cfg = load_yaml(KI_PACKAGE)
    env = jinja2.Environment()
    fehler = [p for p, t in _walk_templates(cfg)
              if _parse_fails(env, t)]
    assert fehler == []


def test_mapping_example_jinja_parst():
    cfg = load_yaml(REPO / "opti_mapping.example.yaml")
    env = jinja2.Environment()
    fehler = [p for p, t in _walk_templates(cfg)
              if _parse_fails(env, t)]
    assert fehler == []


def _parse_fails(env, template_str):
    try:
        env.parse(template_str)
        return False
    except jinja2.TemplateSyntaxError:
        return True
