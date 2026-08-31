"use strict";

// ---------------------------------------------------------------------------
// Startup & Main Entry Point
// ---------------------------------------------------------------------------

addMessage("Hallo! Ich beantworte Fragen zu Status, Problemen und Ideen.", "agent");

void loadOverview();
void loadPublicActivity();
void loadDiagnostics();
setInterval(loadPublicActivity, 10000);
setInterval(loadDiagnostics, 10000);
