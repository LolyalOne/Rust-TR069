# ACS TR-369/USP - Mapa de Desenvolvimento e Handover

> **Última Atualização:** 06/09/2026 - **STATUS: SUBAGENTES INTERROMPIDOS PELO USUÁRIO**
> **Repositório Git Alvo:** `https://github.com/LolyalOne/Rust-TR069.git`
> **Diretório do Projeto (WSL):** `/mnt/d/Projetos/TR069-181`
> **Objetivo:** ACS (Auto Configuration Server) TR-369/USP operando sobre MQTT com arquitetura Event-Driven, limites rígidos de RAM (WSL 6 GB total) e alta tolerância a falhas.

---

## 🛑 Ponto Exato de Parada dos Subagentes (Esclarecimento de Milestones)

Houve a impressão de estarmos no **Milestone 3 (Rust USP Core)** porque a equipe especialista de pesquisa (`spec_miner_usp_1`) já havia concluído e documentado todo o mapeamento técnico do protocolo TR-369 e dos schemas Protobuf. No entanto, devido ao sistema de **Portão de Qualidade Rigoroso (Quality Gate)** do enxame, o código do Milestone 3 ainda não havia sido gerado em disco porque o **Milestone 2 (PostgreSQL)** estava passando pelos ajustes finais da sua 2ª iteração.

Abaixo está a situação real de cada Milestone no momento do encerramento:

---

### ✅ Milestone 1: Infraestrutura & Portabilidade (100% CONCLUÍDO E HOMOLOGADO)
- **Status do Gate:** `PASS` (Aprovado por unanimidade por 3 Revisores, 2 Desafiadores e 1 Auditor Forense).
- **Arquivos entregues e validados em disco:**
  - `docker-compose.yml`: Limites estritos de hardware (Postgres 1.5GB, Mosquitto 500MB, Rust 500MB, FastAPI 1GB) com healthchecks e volume `tmpfs`.
  - `.devcontainer/devcontainer.json`: Tooling completo de VS Code para Rust, Python e Docker.
  - `configure_limits.py`: CLI interativa testada e homologada (10/10 testes unitários).
  - `simulate_flow.sh`: Script mestre de teste E2E ponta a ponta (24 KB de rotinas automatizadas).
  - `mosquitto/mosquitto.conf`: Broker configurado na porta 1883 com persistência.

---

### ⚠️ Milestone 2: Banco de Dados Híbrido PostgreSQL (PAROU NA ITERAÇÃO 2)
- **O que foi feito:** O agente `worker_m2_db` entregou `postgres/init.sql` (7.8 KB), `postgres/test_schema.py` (28 KB) e `postgres/test_reconciliation_empirical.py` (21 KB).
- **Auditoria do Gate (Iteration 1 - FAIL):** O Auditor Forense barrou a primeira versão com 3 apontamentos:
  1. `CREATE TABLESPACE` não pode ser executado dentro de blocos `DO $$ ... $$` no PostgreSQL.
  2. A trigger na tabela `UNLOGGED` continha um `UPDATE` incondicional em `cpe_inventory` que gerava amplificação de escrita de WAL (invalidando a economia de I/O).
  3. Os testes de mock precisavam ser amarrados ao container PostgreSQL real.
- **Onde eles estavam quando pararam:** O orquestrador havia iniciado a **Iteração 2 (M2-It2)** com o agente `explorer_m2_it2_3` para corrigir esses 3 pontos no arquivo `postgres/init.sql`.

---

### 📋 Milestone 3: Rust USP Core Worker (PESQUISA CONCLUÍDA / CÓDIGO PENDENTE DE LIBERAÇÃO)
- **O que foi feito:** O especialista `spec_miner_usp_1` minerou e gerou um relatório exaustivo de 26 KB (`.agents/spec_miner_usp_1/handoff.md`) com:
  - Mapeamento completo dos Protobufs oficiais da Broadband Forum (`usp-record-1-3.proto` e `usp-msg-1-3.proto`).
  - Wire-compatibility para interoperar com agentes USP padrão da indústria (como `obuspa`).
  - Hierarquia de tópicos MQTT (`usp/endpoint/#` e `usp/controller/#`).
  - Arquitetura de canais Tokio MPSC desacoplando a ingestão MQTT da escrita no Postgres.
- **O que ficou pendente:** Como o Milestone 2 ainda não havia recebido o selo `PASS` definitivo na Iteração 2, o orquestrador **ainda não havia criado a pasta `rust-core/` nem escrito os arquivos de código**.

---

### 📋 Milestone 4: Python FastAPI Manager (AGUARDANDO MILESTONE 3)
- Especificação pronta na arquitetura; criação da pasta `python-api/` aguarda a conclusão do Rust Core.

---

## 🚀 Roteiro Imediato para a Próxima I.A.

Qualquer nova I.A. ou desenvolvedor que retomar o projeto deve seguir exatamente esta ordem:

### 1. Finalizar o Milestone 2 no `postgres/init.sql`:
- Garantir que `CREATE TABLESPACE ram_db LOCATION '/var/lib/postgresql/ram_data';` esteja no topo e fora de qualquer transação `DO $$`.
- Na trigger `reconcile_live_to_history`, garantir que ela insira em `cpe_historical_metrics` apenas quando o sinal óptico variar mais de 1.0 dBm, sem executar `UPDATE` em `cpe_inventory`.

### 2. Criar e Codificar o Milestone 3 (`rust-core/`):
- Usar as especificações detalhadas em `.agents/spec_miner_usp_1/handoff.md`.
- Criar `rust-core/Cargo.toml` (`tokio`, `rumqttc`, `prost`, `prost-build`, `sqlx`, `tracing`).
- Criar `rust-core/build.rs` e `rust-core/proto/usp.proto`.
- Criar `rust-core/src/main.rs` com canal Tokio MPSC (`mpsc::channel(200)`).
- Criar `rust-core/Dockerfile` (multi-stage `rust:alpine` -> `alpine:latest`).

### 3. Criar e Codificar o Milestone 4 (`python-api/`):
- Criar `python-api/requirements.txt`, `python-api/app/main.py`, `python-api/gunicorn_conf.py` (2 workers) e `python-api/Dockerfile`.

### 4. Validar e Versionar:
- Subir a infraestrutura: `docker compose up -d --build`.
- Rodar o harness: `./simulate_flow.sh`.
- Fazer o commit e push para `https://github.com/LolyalOne/Rust-TR069.git`.
