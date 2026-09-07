# 📖 Tutorial Definitivo: Como Testar e Usar o ACS Dual-Stack

Este guia prático ensina como validar e utilizar o seu ACS de Gerenciamento de Roteadores, tanto no ambiente controlado do seu computador quanto no ambiente de produção do seu provedor (ISP).

---

## 🌍 Parte 1: Como Funciona no Ambiente Real (Provedor)

No mundo real (produção), você vai colocar este software rodando em uma máquina virtual (VM) ou servidor na nuvem (VPS, AWS, etc.) ou no próprio Datacenter do seu provedor.

### 1. Portas Necessárias
Para que os roteadores encontrem o seu ACS através da internet, seu firewall precisará expor duas portas principais:
- **Porta `7547` (TCP):** Para receber as ONTs tradicionais (Huawei, TP-Link) via **TR-069 Clássico**.
- **Porta `1883` (TCP):** Para receber os roteadores novos (Wi-Fi 6) via **TR-369 USP (MQTT)**.
- **Porta `8000` (TCP):** Acesso à API FastAPI (Apenas para o seu Painel Interno / CRM, **não exponha** para a internet aberta).

### 2. O Fluxo de Conexão das ONTs (TR-069)
1. **Configuração da ONT:** Você configura a URL do ACS no roteador do cliente (ex: `http://acs.meuprovedor.com.br:7547`). Isso pode ser feito via DHCP Option 43, preset (Agile Config) ou configuração manual.
2. **O Aperto de Mão (Inform):** Assim que conecta na internet, a ONT envia um arquivo XML (`Inform`) para a porta 7547.
3. **Rust em Ação:** O nosso motor em Rust lê o XML, extrai o número de série, captura o sinal óptico e joga no banco de dados.
4. **Respostas e Comandos:** Se você solicitou um comando de "Reboot" no painel FastAPI, ele fica guardado no PostgreSQL. Quando a ONT conectar, o Rust lê o banco e devolve o comando XML ordenando a reinicialização.

---

## 💻 Parte 2: Como Testar no Ambiente Local (Seu Computador)

Para validar o funcionamento sem precisar de um roteador real, usaremos o Docker e o terminal.

### Passo 1: Subir toda a infraestrutura
Abra o terminal na pasta raiz do projeto (`Rust-TR069`) e execute:
```bash
docker compose up -d --build
```
*Isso vai iniciar o Banco de Dados, o Mosquitto, a API Python (8000) e o Servidor Rust (7547).*

### Passo 2: Simulando um Roteador TR-069 (Huawei)
Você não precisa de um roteador. Podemos simular o roteador usando o comando `curl` no seu terminal. Copie e cole o bloco inteiro abaixo:

```bash
curl -X POST http://localhost:7547 \
-H "Content-Type: text/xml" \
-d '<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Header><cwmp:ID mustUnderstand="1">10001</cwmp:ID></SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <cwmp:Inform>
      <DeviceId>
        <Manufacturer>Huawei Technologies Co., Ltd.</Manufacturer>
        <OUI>00259E</OUI>
        <ProductClass>EchoLife HG8245H</ProductClass>
        <SerialNumber>485754431234ABCD</SerialNumber>
      </DeviceId>
      <Event soap-enc:arrayType="cwmp:EventStruct[1]">
        <EventStruct><EventCode>1 BOOT</EventCode><CommandKey></CommandKey></EventStruct>
      </Event>
      <ParameterList soap-enc:arrayType="cwmp:ParameterValueStruct[2]">
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.DeviceInfo.SoftwareVersion</Name>
          <Value xsi:type="xsd:string">V5R019C00S105</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.WANDevice.1.WANDSLInterfaceConfig.X_HW_OpticalRxPower</Name>
          <Value xsi:type="xsd:string">-19.50 dBm</Value>
        </ParameterValueStruct>
      </ParameterList>
    </cwmp:Inform>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>'
```
**O que vai acontecer:**
- Você acabou de simular uma ONT Huawei HG8245H ligando na fibra com um sinal de `-19.50 dBm`.
- O servidor Rust (na porta 7547) vai responder com um `<cwmp:InformResponse>` perfeito.
- Os dados foram salvos no banco PostgreSQL em microssegundos.

### Passo 3: Verificando na sua API
Agora, vamos consultar se o roteador fantasma que acabamos de simular realmente apareceu no seu sistema FastAPI:

Acesse no seu navegador ou via curl:
```bash
curl http://localhost:8000/api/v1/cpes/status
```
Você verá o JSON contendo o MAC/Serial (`485754431234ABCD`), o status online e o nível óptico perfeitamente extraído do XML.

### Passo 4: Simulando Roteadores Novos (TR-369 via MQTT)
Se quiser testar a rota moderna (TR-369), basta executar o script automatizado que a equipe desenvolveu. Ele simula todo o tráfego via MQTT:
```bash
./simulate_flow.sh
```

---

## 🎯 Resumo da Operação
- **Para ler relatórios (O que está online?):** Você (ou seu sistema web/n8n) consome a API do Python (`localhost:8000`).
- **Para enviar comandos (Ex: Mudar Wi-Fi):** Você envia para o Python. Se o roteador for TR-369, o Python atira via MQTT. Se o roteador for TR-069, o Python salva no Banco, e o Rust entrega para o roteador no próximo contato.
