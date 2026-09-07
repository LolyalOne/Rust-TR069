# ⚡ Rust-TR069 / TR-369 USP Next-Gen ACS
### *Carrier-Grade, Event-Driven Auto Configuration Server operando sobre MQTT & PostgreSQL Híbrido*

[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Rust](https://img.shields.io/badge/Rust-2021_Edition-orange.svg?logo=rust)](https://www.rust-lang.org/)
[![Tokio](https://img.shields.io/badge/Runtime-Tokio_Async-blueviolet.svg)](https://tokio.rs/)
[![FastAPI](https://img.shields.io/badge/API-FastAPI_Async-009688.svg?logo=fastapi)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/Database-PostgreSQL_Hybrid-336791.svg?logo=postgresql)](https://www.postgresql.org/)
[![MQTT](https://img.shields.io/badge/Broker-Eclipse_Mosquitto-red.svg?logo=eclipse-mosquitto)](https://mosquitto.org/)
[![Docker](https://img.shields.io/badge/Orchestration-Docker_Compose-2496ED.svg?logo=docker)](https://www.docker.com/)

---

## 🌟 Visão do Projeto: A Nova Fronteira do Gerenciamento de Redes e ISPs

O gerenciamento tradicional de dispositivos de telecomunicações (CPEs, ONUs, OLTs, Roteadores Wi-Fi 6) esteve preso por mais de uma década às limitações do protocolo legado **TR-069 (CWMP)**: sessões SOAP/XML pesadas sobre HTTP síncrono, alto consumo de banda, polling ineficiente e latência inaceitável para tomada de decisão em tempo real.

O **Rust-TR069 (USP ACS)** nasce para quebrar esse paradigma. Projetado do zero sob a especificação **TR-369 (User Services Platform - USP)** da *Broadband Forum (BBF)*, este sistema introduz uma **Arquitetura Orientada a Eventos (Event-Driven)** ultrarrápida, resiliente e escalável sobre o broker **MQTT**:

* ⚡ **De Segundos para Milissegundos:** Telemetria em tempo real compactada em **Protocol Buffers (Protobuf)** trafegando via tópicos MQTT.
* 🛡️ **Segurança de Memória e Concorrência Extrema:** O núcleo de processamento MTP é forjado em **Rust puro** com runtime assíncrono Tokio e isolamento de threads por canais MPSC.
* 💾 **Engenharia de Dados Híbrida (RAM + Disco):** Ingestão de milhões de métricas voláteis em tabelas de memória ultrarrápidas (`UNLOGGED` em `tmpfs`), reconciliando com persistência em disco apenas sob anomalias reais de rede.
* 🌐 **SDN / Controller-Agent Desacoplado:** Controle imperativo de CPEs via API RESTful assíncrona (FastAPI) com latência sub-segundo para automações com n8n, CRMs e painéis de operação.
* 🎛️ **Eficiência de Recursos Sem Precedentes:** Projetado para rodar em hardware de laboratório restrito (**WSL 2 com 6 GB de RAM**), pronto para escalar horizontalmente para infraestruturas de telecomunicações de grande porte.

---

## 🏗️ Arquitetura de Alto Nível

```mermaid
graph TD
    subgraph "Borda / Dispositivos (CPEs)"
        CPE1["ONU / Roteador Wi-Fi (CPE 1)"]
        CPE2["ONU / Roteador Wi-Fi (CPE 2)"]
        CPEn["Dispositivo TR-369 (CPE n)"]
    end

    subgraph "Broker MTP (Eclipse Mosquitto 1883)"
        TOPIC_IN["usp/endpoint/{mac}/inform<br/>usp/endpoint/{mac}/notify"]
        TOPIC_OUT["usp/controller/{mac}/operate"]
    end

    subgraph "MTP Core Worker (Rust Tokio)"
        MQTT_CLI["Receptor Assíncrono rumqttc"]
        MPSC["Canal Tokio MPSC<br/>(Backpressure Bounded: 200)"]
        PROST["Decodificador Protobuf (prost)"]
        DB_POOL["Pool de Conexões sqlx (Max 5)"]
    end

    subgraph "PostgreSQL 15 Híbrido"
        RAM_TBL["Tier 1: cpe_live_state<br/>(UNLOGGED em tmpfs RAM)"]
        TRIGGER["Trigger de Reconciliação<br/>(Delta Óptico > 1 dBm)"]
        DISK_TBL["Tier 2: cpe_historical_metrics<br/>cpe_inventory (Disco Persistente)"]
    end

    subgraph "Manager & SDN Controller (FastAPI)"
        API["FastAPI Async Engine (Python 3.11)"]
        GUNICORN["Gunicorn (2 Workers Uvicorn)"]
        PUB["Publicador MQTT (paho-mqtt)"]
    end

    subgraph "Integrações Externas"
        N8N["Automação ISP (n8n / CRM)"]
        DASH["Frontend / Painel NOC"]
    end

    %% Fluxos Upstream (Telemetria)
    CPE1 -->|Protobuf TR-369| TOPIC_IN
    CPE2 -->|Protobuf TR-369| TOPIC_IN
    CPEn -->|Protobuf TR-369| TOPIC_IN
    TOPIC_IN --> MQTT_CLI
    MQTT_CLI --> MPSC
    MPSC --> PROST
    PROST --> DB_POOL
    DB_POOL -->|Zero-WAL Fast Write| RAM_TBL
    RAM_TBL -->|Disparo sob Variação| TRIGGER
    TRIGGER -->|Gravação Histórica| DISK_TBL

    %% Fluxos Downstream (Comandos)
    DASH -->|POST /reboot| API
    N8N -->|POST /cpe| API
    API -->|Consulta em RAM| RAM_TBL
    API --> GUNICORN
    GUNICORN --> PUB
    PUB -->|Ordem de Operação| TOPIC_OUT
    TOPIC_OUT -->|Device.Reboot()| CPE1
```

---

## 🧩 Os 4 Pilares da Engenharia do Sistema

### 1. Ingestão MTP em Rust (`rust-core`)
- **Runtime Tokio Multi-thread:** Ingestão não bloqueante com capacidade de suportar milhares de conexões MQTT concorrentes com footprint de memória ínfimo (< 100 MB).
- **Canal MPSC com Backpressure:** Desacopla estritamente o loop de eventos MQTT da camada de persistência. Caso o banco sofra lentidão, o canal absorve a carga com fila finita e backpressure nativo, impedindo *Out-Of-Memory* (OOM).
- **Protobuf Binário (TR-369 BBF):** Compatibilidade direta com as especificações da Broadband Forum (`usp-record` e `usp-msg`), compatível com agentes de mercado como o `obuspa`.

### 2. Banco de Dados Híbrido (Dual-Tier Persistence)
- **Tier 1 (RAM via `tmpfs`):** Montagem dedicada de 1 GB em `/var/lib/postgresql/ram_data`. A tabela `cpe_live_state` é declarada como `UNLOGGED`, eliminando a escrita em WAL (*Write-Ahead Logging*) para absorver rajadas intensas de telemetria (Uptime, Status, Sinal Óptico RX, Latência).
- **Tier 2 (Persistência em Disco):** A tabela `cpe_inventory` mantém o cadastro confiável de dispositivos. Uma trigger inteligente (`reconcile_live_to_history`) analisa se houve degradação significativa no sinal de fibra óptica ($> 1.0\text{ dBm}$) e, somente nesse caso, descarrega o evento na tabela histórica persistente `cpe_historical_metrics`.

### 3. Manager API & SDN Controller (`python-api`)
- **Python 3.11 + FastAPI + SQLAlchemy 2.0 Assíncrono (`asyncpg`):** Respostas em frações de milissegundo para o operador.
- **Endpoints RESTful:**
  - `POST /api/v1/cpe`: Registro idempotente de CPEs no inventário.
  - `GET /api/v1/cpe/{mac}/status`: Leitura direta em memória da tabela `cpe_live_state`.
  - `POST /api/v1/cpe/{mac}/reboot`: Publicação instantânea de comandos no broker MQTT.
- **Isolamento via Gunicorn:** Configurado estritamente com 2 workers Uvicorn e reciclagem de processos (`max_requests = 1000`) para evitar qualquer vazamento de memória da runtime Python.

### 4. Infraestrutura como Código & Portabilidade Extrema
- **Perfil de Recursos Rígido (WSL 2 / 6 GB RAM Total):**
  - PostgreSQL: Limite de **1.5 GB**
  - Mosquitto MQTT: Limite de **500 MB**
  - Rust USP Core: Limite de **500 MB**
  - Python FastAPI: Limite de **1.0 GB**
  - Host / Buffer: **~2.5 GB** livres
- **DevContainer Homologado:** Configuração pronta em `.devcontainer/devcontainer.json`. Qualquer desenvolvedor sobe o ambiente completo de Rust, Python e Docker em segundos no VS Code, em qualquer sistema operacional.
- **Configurador Dinâmico (`configure_limits.py`):** Utilitário interativo via CLI para reajustar com segurança os limites de hardware no `docker-compose.yml` quando o projeto for implantado em servidores com 32 GB, 64 GB ou mais.

---

## 📂 Estrutura do Repositório

```text
├── .agents/                    # Relatórios, briefings e auditorias técnicas dos subagentes
├── .devcontainer/              # Configuração completa do ambiente VS Code DevContainer
│   └── devcontainer.json       # Extensões para Rust, Python, Docker e TOML
├── mosquitto/                  # Configuração do broker MQTT
│   └── mosquitto.conf          # Listeners na porta 1883 e persistência de mensagens
├── postgres/                   # Engenharia de dados e scripts SQL
│   ├── init.sql                # Tablespace em RAM, schemas híbridos e trigger de reconciliação
│   ├── test_schema.py          # Bateria de testes automatizados do schema
│   └── test_reconciliation_empirical.py # Testes empíricos de reconciliação de sinal
├── rust-core/                  # MTP Worker assíncrono em Rust (Milestone 3)
│   ├── proto/                  # Schemas Protocol Buffers do TR-369 (BBF)
│   ├── src/main.rs             # Runtime Tokio, cliente MQTT e canal MPSC
│   ├── Cargo.toml              # Dependências otimizadas (rumqttc, sqlx, prost)
│   └── Dockerfile              # Multi-stage build (rust:alpine -> alpine final)
├── python-api/                 # Manager API e SDN Controller (Milestone 4)
│   ├── app/main.py             # Rotas REST e orquestração de comandos MQTT
│   ├── requirements.txt        # Dependências assíncronas (FastAPI, SQLAlchemy v2)
│   ├── gunicorn_conf.py        # Configuração de limites de memória para os workers
│   └── Dockerfile              # Imagem enxuta baseada em Python slim
├── configure_limits.py         # Ferramenta interativa de gestão de cotas de RAM
├── simulate_flow.sh            # Harness de testes de integração ponta a ponta (E2E)
├── docker-compose.yml          # Orquestração com limites estritos de hardware e healthchecks
├── HANDOVER_STATUS.md          # Mapa técnico completo de continuidade e handover
└── README.md                   # Esta documentação
```

---

## 🚀 Como Executar o Projeto

### Pré-requisitos
* [Docker](https://docs.docker.com/get-docker/) e Docker Compose v2 instalados.
* [Git](https://git-scm.com/) instalado.

### 1. Clonar o Repositório
```bash
git clone https://github.com/LolyalOne/Rust-TR069.git
cd Rust-TR069
```

### 2. Inicializar os Serviços
Suba os containers com um único comando:
```bash
docker compose up -d --build
```
Os healthchecks em cascata garantirão que o PostgreSQL e o Mosquitto estejam completamente prontos antes do início das aplicações.

### 3. Validar com a Simulação Ponta a Ponta (E2E)
Execute o script de automação para certificar o fluxo completo via terminal:
```bash
chmod +x simulate_flow.sh
./simulate_flow.sh
```
O script testará:
1. Conexão e healthcheck de todos os 4 containers.
2. Cadastro de um CPE de teste via API FastAPI.
3. Ingestão de telemetria simulada via MQTT no Mosquitto.
4. Processamento pelo Worker Rust e gravação direta na RAM (`cpe_live_state`).
5. Disparo da trigger sob variação de sinal óptico para a tabela histórica.
6. Publicação de comando remoto (`Device.Reboot()`) via API de volta no broker MQTT.

### 4. Ajustar Limites de Recursos para Produção
Para escalar os recursos de RAM quando migrar do WSL 2 para um servidor dedicado:
```bash
python3 configure_limits.py
```
*(Você poderá escolher novos limites para o Postgres, Mosquitto, Rust Core e FastAPI de forma assistida)*.

---

## 🗺️ Roadmap de Desenvolvimento

- [x] **Milestone 1 — Infraestrutura & Portabilidade**
  - [x] Docker Compose com restrições rígidas de RAM e volume `tmpfs`.
  - [x] DevContainer com ferramental para Rust, Python e Docker.
  - [x] Script interativo `configure_limits.py` aprovado em testes unitários.
  - [x] Harness de testes E2E `simulate_flow.sh`.
- [x] **Milestone 2 — Banco de Dados Híbrido (PostgreSQL)**
  - [x] Tablespace em RAM `tmpfs` para telemetria volátil.
  - [x] Tabela `cpe_inventory` persistente.
  - [x] Tabela `cpe_live_state` unlogged.
  - [x] Trigger de reconciliação de histórico de degradação de sinal.
- [ ] **Milestone 3 — Rust USP Core Worker**
  - [x] Mapeamento e especificação completa dos schemas Protobuf TR-369 BBF.
  - [ ] Compilação do worker assíncrono Tokio com `rumqttc`, `prost` e `sqlx`.
  - [ ] Canal MPSC com isolamento de backpressure.
- [ ] **Milestone 4 — Python FastAPI Manager**
  - [ ] Endpoints REST assíncronos (CRUD inventário, leitura de RAM, reboot via MQTT).
  - [ ] Configuração do Gunicorn para contenção de RAM < 1 GB.
- [ ] **Milestone 5 — Painel & Automação Avançada**
  - [ ] Painel Web de telemetria óptica em tempo real (React / Vite).
  - [ ] Webhooks e integrações prontas para **n8n** e sistemas de faturamento ISP.

---

## 📚 Referências & Padrões
* [Broadband Forum TR-369 (USP)](https://www.broadband-forum.org/technical/download/TR-369.pdf) — *User Services Platform Architecture & Protocol*.
* [Broadband Forum TR-181](https://usp-data-models.broadband-forum.org/) — *Device Data Model for TR-069/TR-369*.
* [GenieACS](https://github.com/genieacs/genieacs) — *Referência open-source para tratamento de particularidades de CPEs legados*.
* [obuspa (Open Broadband USP Agent)](https://github.com/BroadbandForum/obuspa) — *Agente de referência oficial da BBF*.

---

<p align="center">
  <b>Construído para transformar a operação de ISPs com velocidade, confiabilidade e arquitetura de ponta.</b><br>
  Desenvolvido com o poder da engenharia de sistemas em <b>Rust</b> e <b>Python</b>.
</p>
