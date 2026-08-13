---
type: task
created_by: rene
---

Erweitere die bestehende Weboberfläche um eine übersichtliche Inbox- und Outbox-Ansicht.

Anforderungen:

- Zeige Inbox und Outbox als getrennte Bereiche auf der Startseite.
- Sortiere Einträge jeweils neueste zuerst.
- Zeige mindestens Zeitpunkt, Status, Absender/Empfänger, Titel und eine kurze Inhaltsvorschau, soweit diese Daten vorhanden und sicher freigebbar sind.
- Begrenze jede Liste zunächst auf 20 Einträge.
- Aktualisiere beide Bereiche automatisch alle 10 Sekunden.
- Stelle einen manuellen Aktualisieren-Button bereit.
- Verwende bestehende API-Endpunkte, sofern vorhanden.
- Fehlt ein benötigter Lese-Endpunkt, ergänze einen kleinen, ausschließlich lesenden und streng bereinigten API-Endpunkt.
- Gib keine Secrets, internen Metadaten, vollständigen Prompts oder unbereinigten Fehlermeldungen aus.
- Rendere fremde Inhalte sicher über textContent, nicht über innerHTML.
- Die Darstellung soll auch auf dem Smartphone gut funktionieren.
- Bestehende Funktionen der Website dürfen nicht beeinträchtigt werden.
- Ergänze fokussierte API- und UI-Tests.
- Führe anschließend die vollständige Testsuite aus.

Berichte danach geänderte Dateien, verwendete oder ergänzte Endpunkte, Testergebnisse, Laufzeit sowie offene Probleme oder Sicherheitsrisiken.
