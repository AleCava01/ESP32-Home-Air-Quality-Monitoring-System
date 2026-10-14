#!/bin/bash
set -e
cd "$(dirname "$0")"

docker build -t air-quality-dashboard .
docker stop air-quality-dashboard 2>/dev/null || true
docker rm air-quality-dashboard 2>/dev/null || true

docker run -d --name air-quality-dashboard \
  -p 192.168.1.101:8081:5000 \
  -v /mnt/hdd/sensori:/mnt/hdd/sensori \
  --restart unless-stopped \
  air-quality-dashboard

sleep 2
docker logs --tail 15 air-quality-dashboard
