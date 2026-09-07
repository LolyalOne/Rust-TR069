# Milestone 1 Remediation (Iteration 2) — Technical Analysis & Worker Fix Specifications

**Author**: `explorer_m1_it2_2` (Archetype: `teamwork_preview_explorer`)  
**Target Deliverable**: Fix specifications for Worker implementing Milestone 1 remediation  
**Scope**: 
1. Advisory 1: `start_period: 10s` for PostgreSQL service healthcheck in `docker-compose.yml`.
2. Advisory 2: Space-separated memory limit handling and suffix accumulation prevention in `configure_limits.py`.

---

## 1. Observation

### 1.1 Advisory 1: PostgreSQL Healthcheck Cold-Start Vulnerability
- **Target File**: `/mnt/d/Projetos/TR069-181/docker-compose.yml`, lines 19–24.
- **Current Content**:
  ```yaml
      healthcheck:
        test: ["CMD-SHELL", "pg_isready -U acs_user -d acs_db"]
        interval: 5s
        timeout: 5s
        retries: 5
  ```
- **Comparison with Peer Services in `docker-compose.yml`**:
  - `mosquitto` (lines 38–43): contains `start_period: 5s`
  - `rust-core` (lines 63–68): contains `start_period: 5s`
  - `python-api` (lines 90–95): contains `start_period: 5s`
  - `postgres` (lines 19–24): **lacks `start_period` entirely**.
- **Cold-Start Impact**:
  - `postgres:15-alpine` executes `initdb` and scripts in `/docker-entrypoint-initdb.d/` (specifically `init.sql`, which establishes the RAM tablespace, schema, tables, and triggers) on first container launch.
  - In environments with constrained disk I/O (e.g., Windows WSL2 NTFS mounts), cold-start initialization can take between 15 and 30 seconds.
  - Without `start_period`, the healthcheck begins evaluating immediately at 5s intervals. Each transient failure while `initdb` or `init.sql` executes consumes one of `retries: 5`. If initialization takes > 25 seconds, Docker marks the container `unhealthy`, halting downstream dependent services (`rust-core` and `python-api` configured with `condition: service_healthy`).

### 1.2 Advisory 2: Space-Separated Memory String Suffix Accumulation in `configure_limits.py`
- **Target File**: `/mnt/d/Projetos/TR069-181/configure_limits.py`, lines 22–25, lines 190–198, lines 221–268, lines 270–320.
- **Direct Observations**:
  1. `MEMORY_REGEX` (lines 22–25):
     ```python
     MEMORY_REGEX = re.compile(
         r"^(\d+(?:\.\d+)?)\s*(b|k|m|g|t|p|kb|mb|gb|tb|pb|kib|mib|gib|tib|pib)?$",
         re.IGNORECASE,
     )
     ```
     Because of `\s*`, inputs with whitespace between number and unit (e.g., `"1.5 G"`, `"500 M"`) are accepted as valid by `validate_memory_limit()`.
  2. `update_service_memory_in_text()` (lines 190–196):
     ```python
                     elif in_limits and stripped.startswith("memory:"):
                         # Match pattern: '          memory: <val> # optional comment'
                         match = re.match(r"^(\s*memory:\s*)([^\s#]+)(.*)$", line)
                         if match:
                             line = f"{match.group(1)}{new_limit}{match.group(3)}\n"
                             updated = True
     ```
     In line 192, `([^\s#]+)` matches non-whitespace characters only. When `line` is `          memory: 1.5 G`:
     - Group 1 matches: `'          memory: '`
     - Group 2 matches: `'1.5'` (stops at space `' '`)
     - Group 3 matches: `' G'`
  3. **Empirical Reproduction & Verbatim Error**:
     Executing:
     ```python
     ok1, _, msg1 = modify_service_limit(compose_path, "postgres", "1.5 G")
     # Step 1 succeeds: writes '          memory: 1.5 G' to docker-compose.yml
     ok2, _, msg2 = modify_service_limit(compose_path, "postgres", "2G")
     # Step 2 fails!
     ```
     Verbatim tool output:
     ```
     Step 1 (Set 1.5 G): True Successfully set 'postgres' memory limit to 1.5 G.
     Step 2 (Set 2G): False YAML validation mismatch: expected '2G', found '2G G'.
     ```
     Because Group 3 captured `' G'`, the replacement line synthesized was:
     `{match.group(1)}{new_limit}{match.group(3)}\n` -> `'          memory: 2G G\n'`.
     PyYAML parsed this value as string `'2G G'`, which failed the verification assertion `'2G G' == '2G'`.

---

## 2. Logic Chain

1. **Root Cause Analysis for Advisory 1**:
   - `docker-compose.yml` specifies `condition: service_healthy` on `postgres` for both `rust-core` and `python-api`.
   - PostgreSQL Alpine entrypoint performs multi-stage initialization (cluster creation, superuser setup, database creation, script execution from `/docker-entrypoint-initdb.d/init.sql`).
   - During this window, `pg_isready` fails with exit code 2 ("connection rejected / server starting up").
   - Under Docker healthcheck semantics, `start_period` creates a initialization grace period: healthcheck failures during `start_period` do not decrement the retry budget (`retries: 5`). Once a check succeeds, the container transitions immediately to `healthy`.
   - Adding `start_period: 10s` provides 10s grace + 5 * 5s retries = 35s total tolerance, preventing premature failure during cold-starts without delaying fast boots.

2. **Root Cause Analysis for Advisory 2**:
   - The bug is a compound issue of **unnormalized input persistence** and **greedy non-whitespace regex splitting**:
     - *Issue A (Regex splitting)*: Line 192 assumed memory limits are always whitespace-free tokens matched by `[^\s#]+`. If the line in the file has whitespace (either from a previous update or a manual edit), `[^\s#]+` truncates the value, causing the remainder of the value to leak into Group 3 (intended only for `#` comments).
     - *Issue B (Input normalization)*: Writing unnormalized `"1.5 G"` with spaces into `docker-compose.yml` leaves non-standard formatting in the compose file. Standard Docker Compose memory notation is compact (`1.5G`, `500M`).
   - A single-sided fix (only changing `MEMORY_REGEX` to disallow spaces) would not fix `docker-compose.yml` files if they already contained whitespace from prior manual edits.
   - Therefore, the robust resolution requires a **defense-in-depth fix**:
     1. **Regex Fix**: Update line 192 to match the entire memory value up to the optional `#` inline comment: `r"^(\s*memory:\s*)(.*?)(\s*#.*)?$"`. Group 2 captures the entire value (even if it has spaces or quotes), and Group 3 captures only the comment (e.g. ` # comment`).
     2. **Input Normalization**: Introduce `normalize_memory_limit()` to strip internal whitespace, transforming `"1.5 G"` into canonical `"1.5G"` before writing to disk.

---

## 3. Caveats

- **No Source Code Modifications Made**: In accordance with the Explorer archetype rules, no source files were directly modified in this investigation. All tests were executed on in-memory and temporary mock files.
- **Advisory Scope**: This investigation focuses strictly on the two advisories assigned:
  1. `start_period: 10s` on `postgres` service in `docker-compose.yml`.
  2. Space-separated memory limit handling in `configure_limits.py`.
  Other minor suggestions noted in Milestone 1 reviews (such as inserting limits into arbitrary services lacking `deploy` stanzas or mutual exclusion flags in `argparse`) are non-blocking and out of scope for this remediation.

---

## 4. Conclusion & Actionable Fix Specifications for Worker

### Fix Specification 1: `docker-compose.yml`

#### Target Location
- **File**: `/mnt/d/Projetos/TR069-181/docker-compose.yml`
- **Lines**: 19–24

#### Change Required
Add `start_period: 10s` to the `postgres` healthcheck stanza with 6 spaces of indentation.

#### Before → After Diff
```diff
--- a/docker-compose.yml
+++ b/docker-compose.yml
@@ -19,6 +19,7 @@ services:
     healthcheck:
       test: ["CMD-SHELL", "pg_isready -U acs_user -d acs_db"]
       interval: 5s
       timeout: 5s
       retries: 5
+      start_period: 10s
     networks:
       - acs_network
```

---

### Fix Specification 2: `configure_limits.py`

#### Target Location
- **File**: `/mnt/d/Projetos/TR069-181/configure_limits.py`

#### Changes Required

##### 1. Add `normalize_memory_limit()` Helper
Insert this function around line 107 (immediately after `validate_memory_limit()`):

```python
def normalize_memory_limit(limit_str: str) -> str:
    """Normalize valid memory string to canonical continuous format without spaces (e.g. '1.5 G' -> '1.5G')."""
    match = MEMORY_REGEX.match(limit_str.strip())
    if not match:
        return limit_str.strip()
    unit = match.group(2) or ""
    return f"{match.group(1)}{unit}"
```

##### 2. Fix Line Replacement Regex in `update_service_memory_in_text()`
Replace lines 190–196:

**Before**:
```python
                elif in_limits and stripped.startswith("memory:"):
                    # Match pattern: '          memory: <val> # optional comment'
                    match = re.match(r"^(\s*memory:\s*)([^\s#]+)(.*)$", line)
                    if match:
                        line = f"{match.group(1)}{new_limit}{match.group(3)}\n"
                        updated = True
```

**After**:
```python
                elif in_limits and stripped.startswith("memory:"):
                    # Match pattern: '          memory: <val>  # optional comment'
                    match = re.match(r"^(\s*memory:\s*)(.*?)(\s*#.*)?$", line)
                    if match:
                        trailing_comment = match.group(3) if match.group(3) else ""
                        line = f"{match.group(1)}{new_limit}{trailing_comment}\n"
                        updated = True
```

##### 3. Normalize in `modify_service_limit()`
In `modify_service_limit()`, normalize `new_limit` immediately after validation:

**Before** (lines 224–228):
```python
def modify_service_limit(
    file_path: Path, service: str, new_limit: str, dry_run: bool = False
) -> Tuple[bool, Optional[str], str]:
    """Modify a single service limit and safely write back to file."""
    if not validate_memory_limit(new_limit):
        return False, None, f"Invalid memory limit format: '{new_limit}'. Examples: 500M, 1.5G, 2G"
```

**After**:
```python
def modify_service_limit(
    file_path: Path, service: str, new_limit: str, dry_run: bool = False
) -> Tuple[bool, Optional[str], str]:
    """Modify a single service limit and safely write back to file."""
    if not validate_memory_limit(new_limit):
        return False, None, f"Invalid memory limit format: '{new_limit}'. Examples: 500M, 1.5G, 2G"

    new_limit = normalize_memory_limit(new_limit)
```

##### 4. Normalize in `modify_multiple_limits()`
In `modify_multiple_limits()`, normalize limits in both update and verification loops:

**Before** (lines 280–292 and lines 295–309):
```python
    for service, new_limit in updates.items():
        if not validate_memory_limit(new_limit):
            return False, {}, f"Invalid memory limit '{new_limit}' for service '{service}'."
        if service not in current_limits:
            return False, {}, f"Service '{service}' not found in compose file."

        old_val = current_limits[service]
        content, updated = update_service_memory_in_text(content, service, new_limit)
        if not updated:
            return False, {}, f"Failed to update '{service}'."
        report[service] = (old_val, new_limit)

    # Validate final YAML
    try:
        doc = yaml.safe_load(content)
        for service, new_limit in updates.items():
            verified_val = (
                doc.get("services", {})
                .get(service, {})
                .get("deploy", {})
                .get("resources", {})
                .get("limits", {})
                .get("memory")
            )
            if str(verified_val) != new_limit:
```

**After**:
```python
    for service, raw_limit in updates.items():
        if not validate_memory_limit(raw_limit):
            return False, {}, f"Invalid memory limit '{raw_limit}' for service '{service}'."
        if service not in current_limits:
            return False, {}, f"Service '{service}' not found in compose file."

        new_limit = normalize_memory_limit(raw_limit)
        old_val = current_limits[service]
        content, updated = update_service_memory_in_text(content, service, new_limit)
        if not updated:
            return False, {}, f"Failed to update '{service}'."
        report[service] = (old_val, new_limit)

    # Validate final YAML
    try:
        doc = yaml.safe_load(content)
        for service, raw_limit in updates.items():
            new_limit = normalize_memory_limit(raw_limit)
            verified_val = (
                doc.get("services", {})
                .get(service, {})
                .get("deploy", {})
                .get("resources", {})
                .get("limits", {})
                .get("memory")
            )
            if str(verified_val) != new_limit:
```

##### 5. Add Automated Unit Test to `TestConfigureLimits` in `run_tests()`
Add `test_whitespace_limit_handling` to `TestConfigureLimits` (around line 527):

```python
        def test_whitespace_limit_handling(self):
            with tempfile.TemporaryDirectory() as tmpdir:
                compose_f = Path(tmpdir) / "docker-compose.yml"
                compose_f.write_text(
                    "version: '3.8'\nservices:\n  postgres:\n    deploy:\n      resources:\n        limits:\n          memory: 1.5G  # initial comment\n"
                )
                # Mutation 1: Set limit with whitespace "1.5 G" -> normalized to "1.5G"
                ok1, old1, msg1 = modify_service_limit(compose_f, "postgres", "1.5 G")
                self.assertTrue(ok1, f"Failed setting '1.5 G': {msg1}")
                self.assertEqual(old1, "1.5G")
                limits = get_all_services_and_limits(compose_f)
                self.assertEqual(limits["postgres"], "1.5G")

                # Mutation 2: Set limit to "2G" after whitespace mutation - verifies no suffix accumulation
                ok2, old2, msg2 = modify_service_limit(compose_f, "postgres", "2G")
                self.assertTrue(ok2, f"Failed setting '2G' after '1.5 G': {msg2}")
                self.assertEqual(old2, "1.5G")
                limits = get_all_services_and_limits(compose_f)
                self.assertEqual(limits["postgres"], "2G")

                # Verify comment was preserved
                content = compose_f.read_text()
                self.assertIn("memory: 2G  # initial comment", content)
```

---

## 5. Verification Method

### Step 1: Verify PostgreSQL Healthcheck `start_period`
Execute the following verification command to confirm YAML syntax and `start_period`:
```bash
python3 -c '
import yaml

with open("docker-compose.yml") as f:
    doc = yaml.safe_load(f)

hc = doc["services"]["postgres"]["healthcheck"]
assert hc["start_period"] == "10s", f"Expected start_period: 10s, found {hc.get(\"start_period\")}"
assert hc["test"] == ["CMD-SHELL", "pg_isready -U acs_user -d acs_db"]
assert hc["interval"] == "5s"
assert hc["timeout"] == "5s"
assert hc["retries"] == 5
print("PASS: postgres healthcheck correctly configured with start_period: 10s")
'
```

### Step 2: Run Built-In Unit Test Suite (Including New Whitespace Test)
Execute:
```bash
python3 configure_limits.py --test
```
**Expected Outcome**: 10 tests ran, 0 failures, exit code 0.

### Step 3: Reproduce Challenger 1 Suffix Accumulation Test Case
Execute:
```bash
python3 -c '
import tempfile
from pathlib import Path
from configure_limits import modify_service_limit, get_all_services_and_limits

with tempfile.NamedTemporaryFile("w+", suffix=".yml") as f:
    f.write("services:\n  postgres:\n    deploy:\n      resources:\n        limits:\n          memory: 1.5G\n")
    f.flush()
    p = Path(f.name)
    ok1, _, msg1 = modify_service_limit(p, "postgres", "1.5 G")
    assert ok1, f"Step 1 failed: {msg1}"
    assert get_all_services_and_limits(p)["postgres"] == "1.5G"

    ok2, _, msg2 = modify_service_limit(p, "postgres", "2G")
    assert ok2, f"Step 2 failed: {msg2}"
    assert get_all_services_and_limits(p)["postgres"] == "2G"
    print("PASS: Space-separated input handled cleanly with no suffix accumulation")
'
```
**Expected Outcome**: Exit code 0, `PASS: Space-separated input handled cleanly with no suffix accumulation`.

### Step 4: Verify Full Tool Functionality on Project Compose File
Execute:
```bash
python3 configure_limits.py --show
python3 configure_limits.py --verify
python3 configure_limits.py --preset low --dry-run
python3 configure_limits.py --preset default --dry-run
```
**Expected Outcome**: All commands exit with code 0.
