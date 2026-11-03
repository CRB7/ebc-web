#!/usr/bin/env bash
# Installation sur une VM Debian 13 (ou 12) / Ubuntu fraîche (en root ou via sudo).
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null; then
  echo ">> Installation de Docker"
  apt-get update
  apt-get install -y ca-certificates curl
  # get.docker.com gère Trixie ; repli sur le paquet Debian si le script échoue
  if ! curl -fsSL https://get.docker.com | sh; then
    apt-get install -y docker.io docker-compose
  fi
fi

echo ">> Recherche du port série de l'EBC"
PORT=""
if ls /dev/serial/by-id/* >/dev/null 2>&1; then
  PORT=$(ls /dev/serial/by-id/* | head -n1)
  echo "   trouvé : $PORT"
else
  echo "   aucun port USB-série détecté (EBC branché ? passthrough USB fait ?)"
  echo "   -> démarrage en mode simulateur ; modifie .env plus tard."
  PORT=sim
fi
[ -f .env ] || echo "EBC_PORT=$PORT" > .env

mkdir -p data
if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC=docker-compose; fi
$DC up -d --build
IP=$(hostname -I | awk '{print $1}')
echo
echo ">> OK : http://$IP:8080"
