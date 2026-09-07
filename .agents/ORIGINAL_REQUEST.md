# Original User Request

## Initial Request — 2026-09-07T00:54:06Z

# Teamwork Project Prompt — Draft

> Status: Launched
> Goal: Craft prompt → get user approval → delegate to teamwork_preview
> Requested team: Equipe completa (4 agentes especialistas com base na requisição inicial)

Desenvolver e refinar a arquitetura fundacional e o código-base de um ACS (Auto Configuration Server) TR-369/USP operando sobre MQTT. O sistema deve ser tolerante a falhas, assíncrono e estruturado sob o paradigma de Event-Driven Architecture, consistindo de PostgreSQL, Eclipse Mosquitto, Rust USP Core Worker e Python FastAPI Manager.

Working directory: /mnt/d/Projetos/TR069-181
Integrity mode: development
Reference Material: Caso necessário para lidar com especificidades de compatibilidade TR-069/TR-369 dos roteadores, a equipe está autorizada a pesquisar o repositório open-source do GenieACS no GitHub como referência.

## Requirements

### R1. Infraestrutura em Containers com Limites Físicos
Configuração via Docker Compose alocando recursos estritamente: Postgres (1.5 GB), Mosquitto (500 MB), Rust USP Core (500 MB) e Python FastAPI (1 GB). O Postgres deve usar um volume `tmpfs` para os dados em memória.

### R2. Portabilidade e DevContainer
O projeto deve incluir a pasta `.devcontainer` com a configuração completa (DevContainers) contendo as extensões necessárias para VS Code (Rust, Python, Docker) garantindo que o ambiente híbrido seja reconhecido e carregado automaticamente pelo VS Code em diferentes máquinas físicas.

### R3. Aplicativo de Configuração de Limites
Desenvolver um arquivo de setup ou script interativo (que abra como um aplicativo próprio com interface ou um menu CLI bem estruturado) que permita ao usuário modificar facilmente os limites de memória do `docker-compose.yml`, garantindo que a infraestrutura seja facilmente escalável para computadores melhores.

### R4. Modelo de Dados Híbrido (PostgreSQL)
Tabela persistente `cpe_inventory` e tabela `unlogged` em RAM `cpe_live_state`. Uma função/trigger de reconciliação deve migrar estados validados da memória para tabelas históricas.

### R5. Rust USP Core (Worker) e Protobufs TR-369
Serviço assíncrono (tokio, rumqttc, sqlx) processando mensagens do broker (`usp/endpoint/#`) e gravando estados no banco de dados usando MPSC Channels para desacoplamento. Para a decodificação de payloads via Protobuf (`prost`), a equipe deve baixar os arquivos `.proto` oficiais diretamente do repositório da Broadband Forum (BBF) ou criar um `.proto` de mock mínimo que simule o padrão USP.

### R6. API e Orquestração (Python FastAPI)
API RESTful assíncrona com endpoints CRUD de inventário, consulta de status em tempo real da tabela `unlogged`, e disparo de comandos (ex: Reboot) publicando no broker MQTT.

### R7. Controle de Versão (Git)
Ao final do desenvolvimento e dos testes, a equipe deve inicializar o repositório Git localmente, adicionar o remote `https://github.com/LolyalOne/Rust-TR069.git`, realizar o commit inicial completo de toda a estrutura do projeto e realizar o push.

## Acceptance Criteria

### Infraestrutura, Setup e Portabilidade
- [ ] O DevContainer carrega com sucesso, provendo um ambiente funcional e padronizado.
- [ ] O aplicativo/script de setup lê e modifica os limites de memória no `docker-compose.yml` de forma bem-sucedida, persistindo as mudanças.
- [ ] A execução do `docker-compose up` levanta os 4 serviços com os limites configurados e todos atingem o status `healthy`.

### Verificação Programática e Simulação CLI
- [ ] Existe um script automatizado (ex: `simulate_flow.sh` ou equivalente) capaz de simular os processos inteiros via CLI, executando os seguintes passos sem intervenção humana:
    1. Registrar um CPE de teste através da API FastAPI.
    2. Publicar um payload de telemetria TR-369 via MQTT (Mosquitto).
    3. Validar se o worker Rust consumiu a mensagem e atualizou corretamente a tabela em RAM no banco.
    4. Validar se a alteração de métricas aciona a trigger de reconciliação e salva no histórico.
    5. Disparar um comando via API que publica corretamente a requisição de volta no broker MQTT.
- [ ] A execução desse script de verificação finaliza com 100% de sucesso (código de saída 0), provando a integração da arquitetura.

### Controle de Versão
- [ ] O comando `git remote -v` aponta corretamente para `https://github.com/LolyalOne/Rust-TR069.git`.
- [ ] Todo o código foi "commitado" e submetido (pushed) com sucesso para o repositório.
