# Design: verpflichtendes Cross-Model-Review

Datum: 2026-07-24

## Ziel

Nicht-triviale Pull Requests und umfangreichere Dokumentationsänderungen
erhalten vor dem Merge ein unabhängiges Review durch eine andere
Modellfamilie beziehungsweise einen anderen Anbieter als das primär
implementierende Modell. Die Regel soll für Menschen und Coding-Agenten
verständlich, im Pull Request nachvollziehbar und bei vorübergehend fehlender
Modellverfügbarkeit eindeutig handhabbar sein.

## Geltungsbereich

Das Cross-Model-Review ist verpflichtend für:

- Code-, Test- und Konfigurationsänderungen,
- Home-Assistant-Automationen und andere Änderungen mit Steuerwirkung,
- Deployment-, Sicherheits-, Datenschutz- und Persistenzänderungen,
- umfangreichere Dokumentationsänderungen, insbesondere an Architektur,
  Installation, Betrieb, Wiederherstellung oder Reviewprozessen.

Ausgenommen sind ausschließlich kleine redaktionelle Änderungen wie
Tippfehler, reine Formatierung oder Umformulierungen ohne technische,
prozessuale oder betriebliche Bedeutungsänderung.

## Verankerung

Eine zentrale `REVIEW_POLICY.md` ist die normative Quelle. `AGENTS.md` und
`CLAUDE.md` verpflichten die jeweiligen Coding-Agenten, diese Richtlinie vor
PR-Erstellung und Merge zu befolgen. Eine
`.github/pull_request_template.md` macht die Durchführung oder begründete
Nichtverfügbarkeit im Pull Request sichtbar.

Die drei Einstiegspunkte dürfen die Richtlinie nicht in unterschiedlichen
Varianten duplizieren; sie verweisen auf dieselbe normative Definition.

## Reviewablauf

1. Vor dem Review werden Basis- und Head-Commit, Änderungsumfang,
   Anforderungen und Testnachweise festgelegt. Bei Deployment- oder
   Laufzeitänderungen gehören zusätzlich Live-Nachweise zum Reviewpaket.
2. Das unabhängige Modell arbeitet lesend und bewertet
   Anforderungstreue, Sicherheit, Datenschutz, Testqualität und
   Produktionsreife.
3. Critical- und Important-Findings werden vor dem Merge behoben und erneut
   geprüft. Minor-Findings werden behoben oder im Pull Request dokumentiert.
4. Der Pull Request nennt sowohl das primäre Autor-/Implementierungsmodell
   als auch das Reviewer-Modell, Datum, Commitbereich, Urteil und den Umgang
   mit Findings.

Für den aktuellen Branch ist `OpenAI Codex (GPT-5 family)` das primäre
Autor-/Implementierungsmodell. `claude-opus-4-8` übernimmt das
Cross-Model-Review des finalen Commitbereichs.

## Verfügbarkeit und Ausnahme

Ein installiertes, aber ausgeloggtes Modell gilt nicht sofort als
„nicht verfügbar“. Zunächst ist ein sinnvoller Authentifizierungs- oder
Verbindungsversuch erforderlich.

Kann kein unabhängiges Modell genutzt werden, darf die Ausnahme nur mit einem
konkreten technischen oder organisatorischen Grund im Pull Request verwendet
werden. Die Ausnahme ersetzt nicht das normale Same-Model-Code-Review und
hebt keine bestehenden Test- oder Sicherheitsgates auf.

Die Nichtverfügbarkeit bleibt eine prozedurale, selbst dokumentierte
Ausnahme; dieses Repository prüft sie nicht über eine zusätzliche System-
oder Anbieter-API.

## Erfolgskriterien

- Coding-Agenten finden die Pflichtregel über ihre jeweilige
  Repository-Anweisung.
- Der PR-Text dokumentiert das Cross-Model-Review oder eine konkrete
  Nichtverfügbarkeit.
- Rein redaktionelle Kleinständerungen bleiben ohne unnötiges Review-Gate
  möglich.
- Ein automatisierter Test schützt die verbindlichen Kernaussagen und
  verhindert widersprüchliche oder fehlende Verweise.
- Die exakten Dokument-Fixtures bleiben bewusst bestehen: Frühere Reviews
  haben semantische und Markdown-strukturelle Umgehungen gezeigt, die durch
  bloße Schlagwortprüfungen nicht zuverlässig erkannt würden.
- Das Opus-Review des aktuellen Branches ist vor PR-Erstellung abgeschlossen;
  alle Critical-/Important-Findings sind geschlossen.

## Nicht im Umfang

- Keine Änderung der Home-Assistant-Steuerung oder Live-Konfiguration.
- Keine Festlegung auf einen dauerhaft einzigen Modellanbieter.
- Kein automatischer PR-Merge ohne bestandene Tests und Reviewgates.
