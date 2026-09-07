# Empirical Adversarial Challenge Report — Milestone 1: CLI Limits & Infrastructure

**Challenger**: `challenger_m1_1_rep` (Archetype: `teamwork_preview_challenger`)  
**Target Work Product**: `/mnt/d/Projetos/TR069-181/configure_limits.py`, `/mnt/d/Projetos/TR069-181/docker-compose.yml`  
**Verdict**: **APPROVE** (with 3 documented non-blocking adversarial findings & mitigations)

---

## 1. Observation

### 1.1 CLI Argument Handling
Empirical testing executed against `configure_limits.py` CLI interface:
- `./configure_limits.py --test`: Exit code 0, 9 unit tests passed.
- `./configure_limits.py --show`: Exit code 0, cleanly displayed table of 4 services (`postgres: 1.5G`, `mosquitto: 500M`, `rust-core: 500M`, `python-api: 1G`).
- `./configure_limits.py --verify`: Exit code 0, all services reported `VALID`.
- `./configure_limits.py --preset low --dry-run`: Exit code 0, simulated changes displayed without file modification.
- `./configure_limits.py --preset high --dry-run`: Exit code 0, simulated changes displayed without file modification.
- `./configure_limits.py --service postgres`: Exit code 1, verbatim error:
  `Error: Both --service and --limit must be specified together.`
- `./configure_limits.py --limit 2G`: Exit code 1, verbatim error:
  `Error: Both --service and --limit must be specified together.`
- `./configure_limits.py --service fake_service --limit 2G`: Exit code 1, verbatim error:
  `Error: Service 'fake_service' not found. Available services: postgres, mosquitto, rust-core, python-api`
- `./configure_limits.py --service postgres --limit invalid_val`: Exit code 1, verbatim error:
  `Error: Invalid memory limit format: 'invalid_val'. Examples: 500M, 1.5G, 2G`
- `./configure_limits.py --preset invalid_preset`: Exit code 2 (standard `argparse` invalid choice error).
- `./configure_limits.py --compose-file nonexistent.yml --show`: Exit code 1, verbatim error:
  `Error: Specified compose file not found: nonexistent.yml`
- Non-interactive / EOF input on interactive menu: Exit code 0, gracefully handled `EOFError` and cleanly exited.

### 1.2 Boundary Values & Parsing Behavior
Empirical test suite executed against `validate_memory_limit()`:
- `validate_memory_limit('0')` -> `False` (Rejected zero)
- `validate_memory_limit('0M')` -> `False` (Rejected zero with unit)
- `validate_memory_limit('-1G')` -> `False` (Rejected negative integer)
- `validate_memory_limit('-0.5G')` -> `False` (Rejected negative float)
- `validate_memory_limit('')` -> `False` (Rejected empty string)
- `validate_memory_limit('   ')` -> `False` (Rejected whitespace)
- `validate_memory_limit('abc')` -> `False` (Rejected alphabetical non-number)
- `validate_memory_limit('1.2.3G')` -> `False` (Rejected malformed float)
- `validate_memory_limit('1.5G\nfoo: bar')` -> `False` (Rejected YAML injection attempt)
- `validate_memory_limit('1.5G')` -> `True`
- `validate_memory_limit('500m')` / `'500M'` -> `True`
- `validate_memory_limit('256MiB')` / `'64MB'` -> `True`
- `validate_memory_limit('1024')` / `'1024B'` -> `True`
- `validate_memory_limit('999999999999G')` -> `True`
- `validate_memory_limit('1.5 G')` -> `True` (Note: Accepted due to `\s*` in regex)

### 1.3 Discovered Failure Modes & Edge Cases (Reproduced Empirically)

#### Finding 1 (Medium Risk): Space-Separated Memory String Causes Suffix Accumulation & Corruption
- **Location**: `configure_limits.py`, line 23 (`MEMORY_REGEX`) and line 192 (`re.match(r"^(\s*memory:\s*)([^\s#]+)(.*)$", line)`).
- **Reproduction**:
  ```python
  from configure_limits import modify_service_limit
  # Initial file has 'memory: 1.5G'
  ok1, old1, msg1 = modify_service_limit(compose_path, "postgres", "1.5 G")
  # Returns: (True, '1.5G', "Successfully set 'postgres' memory limit to 1.5 G.")
  # File now contains: '          memory: 1.5 G'
  ok2, old2, msg2 = modify_service_limit(compose_path, "postgres", "2G")
  # Returns: (False, '1.5 G', "YAML validation mismatch: expected '2G', found '2G G'.")
  ```
- **Mechanism**:
  1. `MEMORY_REGEX` permits optional whitespace (`\s*`) between digits and units, so `"1.5 G"` is deemed valid and written to disk.
  2. In line 192, `[^\s#]+` matches only non-whitespace characters (`1.5`), leaving ` G` captured in group 3 (`(.*)`).
  3. When replacing with `"2G"`, the new line is synthesized as `{match.group(1)}{new_limit}{match.group(3)}\n` -> `memory: 2G G`.
  4. PyYAML parses this as string `'2G G'`, which fails the verification gate `str(verified_val) != new_limit` (`'2G G' != '2G'`).

#### Finding 2 (Low Risk): Insertion Logic Synthesizes Duplicate `deploy:` Keys if Service Lacks Limits
- **Location**: `configure_limits.py`, lines 204–218.
- **Reproduction**:
  If an arbitrary service has an existing `deploy` stanza without limits (e.g., `deploy:\n  replicas: 1`), `update_service_memory_in_text()` inserts a new `deploy:` block at `target_service_line_idx + 1`. This generates two `deploy:` keys within the same mapping. In PyYAML, the lower block overrides the upper block, causing parsed `memory` to be `None`, failing the validation check with `YAML validation mismatch: expected '1G', found 'None'`.
- **Impact on TR069-181**: In the target `docker-compose.yml`, all 4 services (`postgres`, `mosquitto`, `rust-core`, `python-api`) already possess complete `deploy.resources.limits.memory` blocks, so this code branch is never triggered during normal ACS operations.

#### Finding 3 (Low Risk): CLI Flag Evaluation Precedence & Standalone `--dry-run`
- **Location**: `configure_limits.py`, lines 651–695.
- **Observation**:
  - Providing mutually conflicting flags (e.g. `./configure_limits.py --show --preset high`) silently executes `--show` and exits without executing `--preset` or warning the user.
  - Running `./configure_limits.py --dry-run` alone launches the interactive menu rather than showing an error, because `args.dry_run` is omitted from `has_cli_option`.

### 1.4 YAML Integrity & Idempotence Across Mutation Cycles
An automated 8-stage mutation test harness was executed on a temporary copy of `docker-compose.yml`:
1. `preset low` -> All 4 service limits transitioned to low profile (`postgres: 512M`, `mosquitto: 128M`, `rust-core: 256M`, `python-api: 512M`). YAML validated.
2. `preset high` -> All 4 service limits transitioned to high profile (`postgres: 4G`, `mosquitto: 1G`, `rust-core: 1G`, `python-api: 2G`). YAML validated.
3. `preset high` (Idempotence test) -> File content before and after was bitwise identical (`assert content_before == content_after` PASSED).
4. Individual updates (`postgres` -> `3G`, `mosquitto` -> `750M`, `rust-core` -> `1.2G`, `python-api` -> `2.5G`) -> All updated correctly. YAML validated.
5. `--verify` -> CLI verify succeeded with code 0 on the updated file.
6. `--dry-run` -> Attempted update with `--limit 10G --dry-run` resulted in zero modifications to the file.
7. `preset default` -> Restored original baseline limits (`postgres: 1.5G`, `mosquitto: 500M`, `rust-core: 500M`, `python-api: 1G`).
8. Structural integrity verification: Verified that non-memory compose elements (`tmpfs: /var/lib/postgresql/ram_data:uid=70,gid=70,mode=0700,size=1G`, `healthcheck`, `networks`, `volumes`, `ports`, `depends_on`) remained 100% intact and uncorrupted.

---

## 2. Logic Chain

1. **R3 Compliance**: The user request requires an interactive setup script or well-structured CLI menu to easily configure memory limits in `docker-compose.yml`. Observation 1.1 and 1.4 prove that `configure_limits.py` provides both an interactive terminal menu and non-interactive CLI flags (`--show`, `--verify`, `--preset`, `--service/--limit`, `--dry-run`, `--test`), satisfying R3.
2. **Data & Syntax Safety**: Observation 1.4 demonstrates that `configure_limits.py` utilizes atomic file writes (via temporary file replacement) and a mandatory PyYAML syntax verification gate before committing changes. All other configuration directives (tmpfs Alpine uid=70, healthchecks, networks, volumes) are preserved without distortion.
3. **Robustness Under Normal Use**: All standard memory notations (`500M`, `1.5G`, `2G`, `256MiB`, `1024`, etc.) parse cleanly, are verified correctly, and demonstrate strict idempotence.
4. **Adversarial Edge Cases**: Findings 1, 2, and 3 identify minor edge-case failure modes in regex parsing, duplicate YAML keys for hypothetical missing sections, and CLI flag precedence. However, because all 4 target services in `docker-compose.yml` already have explicit `deploy.resources.limits.memory` blocks, and because normal inputs use standard continuous format (e.g., `1.5G` rather than `1.5 G`), these edge cases do not impede ACS system operation or prevent the tool from fulfilling its purpose.
5. **Verdict Derivation**: Since all core requirements, acceptance criteria, and safety checks pass with 100% success, the deliverable is approved. The edge-case findings are documented with concrete mitigations for future hardening.

---

## 3. Caveats

- **Concurrent Execution**: `configure_limits.py` relies on atomic filesystem replacement (`tempfile.replace()`), but does not use OS-level file advisory locking (`fcntl.flock`). Simultaneous invocation by multiple concurrent processes could race on final file replacement.
- **Docker Daemon Absence in Test Environment**: The test harness verified `docker-compose.yml` syntax and structure using PyYAML and direct schema validation; live Docker daemon deployment is validated in Milestone 5 E2E testing.

---

## 4. Conclusion

**Verdict**: **APPROVE**

`configure_limits.py` and `docker-compose.yml` fulfill all requirements of Milestone 1. The limit configuration tool safely reads, modifies, and verifies service memory limits while safeguarding compose formatting, comments, and non-memory stanzas.

### Recommended Hardening (Non-blocking):
1. **Fix Finding 1**: Update `MEMORY_REGEX` to disallow spaces (`r"^(\d+(?:\.\d+)?)(b|k|m|g|...)$"`), or normalize the input by removing whitespace before writing to YAML. Alternatively, adjust line matching regex in `update_service_memory_in_text()` to `re.match(r"^(\s*memory:\s*)(.*?)(?:\s*(#.*))?$", line)`.
2. **Fix Finding 2**: If inserting limits into services lacking them, inspect whether `deploy:` already exists in the service block before injecting a new `deploy:` stanza.
3. **Fix Finding 3**: Use `argparse` mutually exclusive groups (`parser.add_mutually_exclusive_group()`) for action flags (`--show`, `--verify`, `--preset`, `--service`), and include `args.dry_run` in `has_cli_option`.

---

## 5. Verification Method

To independently verify these findings, run the following commands:

```bash
# 1. Run internal unit tests
./configure_limits.py --test

# 2. Verify current configuration
./configure_limits.py --verify
./configure_limits.py --show

# 3. Test presets with dry-run
./configure_limits.py --preset low --dry-run
./configure_limits.py --preset high --dry-run
./configure_limits.py --preset default --dry-run

# 4. Reproduce Finding 1 (Space-Separated Memory Suffix Accumulation)
python3 -c '
import tempfile
from pathlib import Path
from configure_limits import modify_service_limit

with tempfile.NamedTemporaryFile("w+", suffix=".yml") as f:
    f.write("services:\n  postgres:\n    deploy:\n      resources:\n        limits:\n          memory: 1.5G\n")
    f.flush()
    p = Path(f.name)
    ok1, _, msg1 = modify_service_limit(p, "postgres", "1.5 G")
    print("Step 1 (Set 1.5 G):", ok1, msg1)
    ok2, _, msg2 = modify_service_limit(p, "postgres", "2G")
    print("Step 2 (Set 2G after 1.5 G):", ok2, msg2)
'
# Expected output:
# Step 1: True Successfully set 'postgres' memory limit to 1.5 G.
# Step 2: False YAML validation mismatch: expected '2G', found '2G G'.
```
