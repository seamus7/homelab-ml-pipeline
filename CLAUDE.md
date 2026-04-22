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
Docker Compose stack has been decomissioned.  Running entirely on k3s now.
Postgres first — everything else depends on it.

## Environment Variables
Sensitive values are in .env — never hardcode credentials in manifests.
Use k8s Secrets instead.

## Auto-Capture Skill

When I say things like "wrap up", "park this", "goodnight", "let's stop here",
or otherwise signal a session is ending:

1. Check Open Brain for obvious duplicates using search_thoughts before capturing
2. Capture each ACT NOW item as its own thought — include the idea, why it matters,
   2-3 next actions, and today's date
3. Capture one session summary — what we worked on, key decisions, main themes
4. Skip low-value noise, raw transcript text, and obvious duplicates

Use the capture_thought and search_thoughts tools from the open-brain connector.

## Work Operating Model Skill

When asked to run the Work Operating Model workflow or build an operating model:

1. First identify all required tools: search_thoughts, capture_thought, start_operating_model_session, save_operating_model_layer, query_operating_model, generate_operating_model_exports. Stop if any are missing.

2. Use this fixed layer order: operating rhythms → recurring decisions → dependencies → institutional knowledge → friction.

3. Start concrete — ask about last week, recent examples, recent misses. Never open with abstract questions.

4. Search Open Brain before each layer for hints. Treat retrieved context as tentative, not fact.

5. Show a checkpoint summary after each layer. Wait for explicit confirmation before calling save_operating_model_layer.

6. Capture one concise summary thought per approved layer via capture_thought.

7. After all five layers: run a contradiction pass with query_operating_model, resolve any conflicts, then call generate_operating_model_exports.

8. Capture one final synthesis thought after export generation.

Never save a layer without explicit user confirmation. Never smooth contradictions silently.
