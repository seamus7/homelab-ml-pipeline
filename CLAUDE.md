# Homelab ML Pipeline — k8s Migration

## Project Context
Migrating a Docker Compose stack to k3s on Ubuntu. Single node for now,
second node (RTX 4080) joining later.

## Stack
- Postgres 15 (shared DB for Gitea and MLflow)
- Gitea (self-hosted Git)
- MLflow 3.10.1 (experiment tracking)
- Ollama (local LLM inference, RTX 5090 GPU)
- Gitea Act Runner (CI)

## Conventions
- Manifests live in k8s/
- Namespace: default for now, may split later
- Storage: local-path provisioner (k3s default)
- Ingress: Traefik (k3s default)
- Hostnames: *.homelab.local
- Server IP: 10.73.1.145

## Current State
Docker Compose stack is still running alongside k3s.
Migrating one service at a time, validating before proceeding.
Postgres first — everything else depends on it.

## Environment Variables
Sensitive values are in .env — never hardcode credentials in manifests.
Use k8s Secrets instead.
