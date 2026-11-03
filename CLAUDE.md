# EBC-A20 Web — contexte pour Claude Code

Interface web (FastAPI + JS vanilla) pour piloter un testeur de batteries ZKETECH EBC-A20
via son port USB-série. Déployée en Docker dans une VM Debian 13 sous Proxmox (USB passthrough).
Utilisateur francophone : interface, messages et commentaires en français.

## Architecture

- `app/protocol.py` — protocole série (encodage/décodage des trames). Aucune dépendance.
- `app/device.py` — `SerialDevice` (pyserial, 9600 8O1) et `SimDevice` (simulateur de cellule 18650).
- `app/engine.py` — thread unique propriétaire du port : connexion/reconnexion, exécution des
  programmes multi-étapes (charge CC-CV, décharge CC/CP/CR, pause, boucle), intégration Ah/Wh,
  estimation R interne, garde-fous logiciels. `LIMITS` = bornes de sécurité.
- `app/db.py` — SQLite (programs, tests, samples, step_results). Chemin via `EBC_DB`.
- `app/main.py` — API REST + WebSocket `/ws` (événements status / sample / log / test_finished).
- `app/static/` — `index.html`, `app.js`, `style.css`, uPlot 1.6.32 vendorisé (pas de CDN : la VM peut être hors ligne).

Les threads web parlent au moteur uniquement via `engine.submit(...)` (file de commandes) ;
ne jamais écrire sur le port série depuis une route HTTP.

## Protocole EBC-A20 (résumé)

Sources : github.com/dev-strom/esp-ebc-mqtt (`lib/commands/EbcA20.cpp`) et github.com/ardiehl/EBC-controller (`main.pas`, `ebcc.init`).

- Commande PC→EBC, 10 octets : `FA cmd p1h p1l p2h p2l p3h p3l crc F8`
- Réponse EBC→PC, 19 octets, ~1/s : `FA mode v0..v6 model_id crc F8`
- Valeurs en base 240 (`hi*240+lo`). CRC = XOR des octets entre FA et crc ; si ≥ 0xF0, garder le quartet bas.
- Commandes : 0x05 connect, 0x06 disconnect, 0x02 stop, 0x21 charge CC-CV (A, V, A fin, en centièmes),
  0x01 décharge CC (A, V coupure, minutes), 0x11 décharge CP (W, V, minutes), 0x07 ajuste une décharge CC en cours.
- Modes réponse : unités = type (0 D-CC, 1 D-CP, 2 C-CV), dizaines = état (0 arrêt, 1 en cours, 2 terminé).
  Les modes ≥ 100 sont des variantes de rôle inconnu, ramenées à la base.
- Tension réponse en mV, courant en cA, capacité : < 10 Ah en mAh, sinon bit 0x8000 + (n - 0x0800)/100.
- ID modèle A20 = 0x09.

`tests/test_protocol.py` vérifie l'encodage contre des trames réelles capturées par ces projets.

## Points non vérifiés (à traiter avec prudence)

- **Jamais testé sur un vrai EBC-A20** avec ce code. Toute modif du protocole doit garder les tests de trames réelles verts.
- `LIMITS` (charge ≤ 18 V, ≤ 85 W) tirés des specs trouvées, à confirmer avec la notice.
- Décodage tension > 10 V non confirmé (cf. TODO dans EbcA20.cpp).
- Un redémarrage pendant un test le marque `interrupted` ; pas de reprise.
- Pas d'authentification (usage réseau local).

## Commandes

```bash
pip install -r requirements.txt pytest httpx
python -m pytest -q                                   # tests (protocole + moteur sur simulateur)
EBC_PORT=sim EBC_DB=./data/ebc.db uvicorn app.main:app --reload --port 8080   # dev sans matériel
docker compose up -d --build                          # prod (EBC_PORT dans .env)
```

`EBC_SIM_SPEED` accélère le temps simulé (défaut 20).
