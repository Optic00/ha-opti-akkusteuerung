# Entwicklung und Prüfung

GitHub ist die öffentliche Entwicklungsquelle. Die YAML-Vorgänger bleiben ausschließlich im Legacy-Branch. Laufzeitcode und alle Ressourcen liegen unter `custom_components/opti_akku`; private Konfigurationen, Handoffs und Messungen gehören nicht ins Repository.

Mit Python 3.14.2 oder neuer:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-test.txt
ruff check custom_components tests tools
python tools/build_strategy_resources.py --check
pytest --timeout=60
python tools/build_package.py
```

Die Suite prüft unter anderem HA-Lifecycle, Konfigurationsdialoge, Migration, native Modbus-Kommunikation gegen einen lokalen Simulator, Schreibsperren, Strategie, Quellen, Prognosen und den Import eines entpackten Pakets ohne Quellcheckout. Sie ersetzt keine Geräteabnahme.

Änderungen an `strategy/*.yaml` mit `python tools/build_strategy_resources.py` bauen und das JSON mit committen. Der historische Importer ist kein regulärer Buildschritt. HACS lädt das Repository-Verzeichnis; das manuelle ZIP enthält den Pfad `custom_components/opti_akku/` und ist kein aktiviertes `zip_release`-Asset.

Die CI prüft zusätzlich hassfest und die HACS-Anforderungen. Vor folgenreichen Änderungen gilt die [Review-Regel](../REVIEW_POLICY.md). Version in Manifest, Projektmetadaten und Release-Tag synchron halten. Vorabversionen bleiben ausdrücklich Beta; ein grüner Testlauf ist kein Hardware- oder Langzeitnachweis.
