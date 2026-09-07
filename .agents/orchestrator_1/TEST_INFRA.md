# E2E Test Infra: TR-369 / USP ACS

## Test Philosophy
- Opaque-box, requirement-driven. No dependency on internal implementation details.
- Validates the entire event-driven architecture via external entry points: Docker Compose status, HTTP REST API, MQTT broker publishing/subscribing, and CLI configuration tools.
- Methodology: Category-Partition + Boundary Value Analysis + Pairwise Interaction + Real-World Workload Testing.

## Feature Inventory & Test Coverage Goals
| # | Feature | Source (requirement) | Tier 1 (Functional) | Tier 2 (Boundary) | Tier 3 (Cross-Feature) |
|---|---------|---------------------|:-------------------:|:-----------------:|:----------------------:|
| 1 | Memory Limits & tmpfs | ORIGINAL_REQUEST §R1 | 5 | 5 | ✓ |
| 2 | DevContainer Portability | ORIGINAL_REQUEST §R2 | 5 | 5 | ✓ |
| 3 | Limit Configuration CLI | ORIGINAL_REQUEST §R3 | 5 | 5 | ✓ |
| 4 | Hybrid PostgreSQL Model | ORIGINAL_REQUEST §R4 | 5 | 5 | ✓ |
| 5 | Rust USP Core & Protobuf | ORIGINAL_REQUEST §R5 | 5 | 5 | ✓ |
| 6 | FastAPI Manager & MQTT | ORIGINAL_REQUEST §R6 | 5 | 5 | ✓ |
| 7 | Git Setup & Remote Push | ORIGINAL_REQUEST §R7 | 5 | 5 | ✓ |

## Test Architecture
- **Runner**: Bash / Python CLI automated test harness (`simulate_flow.sh` and test verification suites).
- **Semantics**: Strict exit code 0 on full success; non-zero exit code with detailed diagnostic assertions on any failure.
- **Verification Channels**:
  - Container health status: `docker compose ps --format json`
  - REST endpoints: `curl` / `httpx` against `http://localhost:8000`
  - MQTT broker messaging: `mosquitto_pub` and `mosquitto_sub` on port 1883
  - Database schema & tablespace: `psql` inspecting `ram_tablespace`, `cpe_inventory`, `cpe_live_state`, `cpe_state_history`
  - Memory limit persistence: verifying `docker-compose.yml` modifications via `configure_limits.py`

## Real-World Application Scenarios (Tier 4)
| # | Scenario | Features Exercised | Complexity |
|---|----------|--------------------|------------|
| 1 | Full Device Lifecycle & Telemetry Ingest | F1, F4, F5, F6 | High |
| 2 | Metric Alteration Trigger Verification | F4, F5, F6 | High |
| 3 | Remote Reboot Command Round-Trip | F1, F5, F6 | High |
| 4 | Rapid Scale Memory Limit Adjustment | F1, F3 | Medium |
| 5 | Clean Cold-Start Bootstrap to Healthy State | F1, F2, F3, F4, F5, F6 | High |

## Acceptance Verification: simulate_flow.sh
Mandatory 5-step automated CLI sequence:
1. Register test CPE via FastAPI (`POST /api/v1/cpes`).
2. Publish TR-369 telemetry via MQTT Mosquitto (`usp/endpoint/{cpe_id}/telemetry` or `notify`).
3. Validate Rust worker consumed and updated RAM table (`cpe_live_state`).
4. Validate metric change fired reconciliation trigger and recorded historical snapshot (`cpe_state_history`).
5. Dispatch command via FastAPI (`POST /api/v1/cpes/{cpe_id}/reboot`) and verify MQTT capture on broker.
Result must exit with code 0.
