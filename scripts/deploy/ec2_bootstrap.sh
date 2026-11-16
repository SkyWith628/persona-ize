#!/usr/bin/env bash
# EC2(Ubuntu 24.04) 첫 설정: Docker와 compose 설치, 스왑 2GB. 한 번만 실행한다.
#   sudo bash scripts/deploy/ec2_bootstrap.sh
# 이미지 빌드(torch 설치) 때 메모리가 모자라지 않게 스왑을 둔다. 이미 있으면 건너뛴다.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "sudo로 실행하세요." >&2
  exit 1
fi

apt-get update -y
apt-get install -y docker.io docker-compose-v2 git
systemctl enable --now docker

if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# 로그인 사용자가 sudo 없이 docker를 쓰게 한다 (다시 로그인해야 적용).
if [[ -n "${SUDO_USER:-}" ]]; then
  usermod -aG docker "$SUDO_USER"
fi

docker --version
docker compose version
echo "완료. 다시 로그인한 뒤 docs/deploy.md의 '올리기'로 넘어가세요."
