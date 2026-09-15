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

## Versionen und Releases

Versionen folgen dem Veröffentlichungsmonat: `2026.9-beta1`, `2026.9-beta2`,
danach `2026.9.0` stabil und `2026.9.1` für eine Fehlerkorrektur. Die Jahres- und
Monatsangabe bezeichnet Opti Akku, nicht die erforderliche Home-Assistant-Version.
Manifest, `pyproject.toml` und Release-Tag müssen exakt übereinstimmen. Vorhandene
Releases werden nicht nachträglich ersetzt; Korrekturen erhalten eine neue Version.

Vor der Veröffentlichung müssen die PR-Checks und das unabhängige Review
abgeschlossen sein. `python tools/build_package.py --tag 2026.9-beta1` prüft den
Versionsvertrag und baut ein reproduzierbares ZIP ausschließlich aus eingecheckten
Komponentendateien. Das ZIP wird für manuelle Installationen in das HA-Konfigurationsverzeichnis
entpackt; es enthält bereits `custom_components/opti_akku/`.

Nach dem Merge wird ein annotierter Tag auf den geprüften Commit gepusht. Der
Release-Workflow prüft den Versionsvertrag, baut das ZIP zweimal mit identischen
Bytes und erzeugt einen **Release-Entwurf samt ZIP**. Betas erhalten das Prerelease-Kennzeichen.
Erst nach erfolgreichem Workflow und Prüfung der Release-Notizen wird der Entwurf
veröffentlicht. Ein erneuter Lauf ersetzt kein bestehendes Release. Nach Prüfung eines gescheiterten
Laufs kann **Prepare release → Run workflow** auf `main` denselben bestehenden
annotierten Tag erneut vorbereiten; der Tag wird dabei nicht verändert.

HACS lädt weiterhin den Komponentenordner aus dem gewählten Tag. Das zusätzliche
ZIP ist für manuelle Installation; `zip_release` bleibt deshalb aus.
Anschließend Versionsauswahl und Download in HACS
prüfen. Release-Notizen nennen HA-Mindestversion, Änderungen, bekannte Grenzen
sowie Backup und ausgeschaltete Schreibfreigabe vor einem Update.
