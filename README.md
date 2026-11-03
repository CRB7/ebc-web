# EBC-A20 Web

Interface web pour le testeur de batteries ZKETECH EBC-A20, à faire tourner dans une VM (Proxmox) ou sur n'importe quel Linux avec Docker.

- **Direct** : tension, courant, puissance, capacité, énergie, R interne, graphe temps réel.
- **Programmes** : charge CC-CV, décharge CC / CP / CR (logiciel), pauses, boucles. 4 programmes prédéfinis, dupliquables.
- **Historique** : tous les tests en SQLite, détail par étape, export CSV (séparateur `;`, ouvre direct dans Excel/LibreOffice FR).
- **Comparaison** : classement par étiquette de batterie + courbes de décharge superposées.

## 1. Proxmox : créer la VM et passer l'USB

1. Crée une VM Debian 13 « Trixie » (1 vCPU, 1 Go RAM, 8 Go disque suffisent). Un conteneur LXC marche aussi mais le passthrough USB y est plus pénible.
2. Branche l'EBC sur l'hôte Proxmox et identifie-le :
   ```
   lsusb
   ```
   Repère la ligne du convertisseur USB-série (souvent `1a86:7523` CH340 ou `10c4:ea60` CP210x — vérifie, je ne connais pas avec certitude la puce de ton A20).
3. Passe-le à la VM **par identifiant** (survit au changement de port USB) :
   ```
   qm set <VMID> -usb0 host=1a86:7523
   ```
   ou via l'interface : VM → Hardware → Add → USB Device → *Use USB Vendor/Device ID*.
4. Dans la VM : `ls /dev/serial/by-id/` doit afficher l'appareil.

## 2. Installer dans la VM

```bash
apt install -y unzip
unzip ebc-web.zip && cd ebc-web
sudo ./install.sh
```

Le script installe Docker, détecte le port série, écrit `.env` et démarre. Ouvre ensuite `http://<ip-de-la-vm>:8080`.

Sans EBC branché, il démarre en **simulateur** (cellule 18650 virtuelle, temps accéléré ×20) : pratique pour tester l'interface.

Configuration dans `.env` :
```
EBC_PORT=/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0   # ou sim
EBC_SIM_SPEED=20
```
Puis `docker compose up -d`.

Les données sont dans `./data/ebc.db` : sauvegarde ce fichier.

## 3. Utilisation

1. **Programmes** : choisis un prédéfini ou crée le tien. *Durée max* et *Ah max* sont des garde-fous logiciels optionnels.
2. **Direct** : choisis le programme, donne une **étiquette de batterie** (ex. `18650-A03`) — c'est elle qui sert au classement — puis *Démarrer*.
3. La R interne est estimée au début de chaque décharge : (tension au repos − tension sous charge) / courant. Elle n'est fiable que si une **pause** précède la décharge.

## Mise à jour / arrêt

```
docker compose up -d --build   # après modification du code
docker compose logs -f         # journal
docker compose down            # arrêt
```

## Limites connues

- Protocole reconstitué depuis [esp-ebc-mqtt](https://github.com/dev-strom/esp-ebc-mqtt) et [EBC-controller](https://github.com/ardiehl/EBC-controller), vérifié contre leurs trames capturées, **pas encore testé sur un vrai A20 avec ce code**.
- Limites appliquées : charge 0,1–5 A et ≤ 18 V, décharge 0,1–20 A, ≤ 85 W. Les valeurs de tension max de charge et de puissance sont à confirmer sur ta notice (`LIMITS` dans `app/engine.py`).
- Si la VM ou le service redémarre pendant un test, le test est marqué *interrompu* ; il ne reprend pas automatiquement. L'EBC, lui, continue sa consigne en cours.
- Pas d'authentification : à garder sur le réseau local.

## Développement

```
pip install -r requirements.txt pytest httpx
EBC_PORT=sim EBC_DB=./data/ebc.db uvicorn app.main:app --reload --port 8080
python -m pytest -q
```
