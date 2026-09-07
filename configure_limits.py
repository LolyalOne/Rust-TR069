#!/usr/bin/env python3
"""
TR-369 / USP ACS Resource Limit Configuration Tool
Requirement R3: Interactive & CLI memory limit setup tool for docker-compose.yml
"""

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:
    print("Error: PyYAML is required. Please install it via 'pip install pyyaml'.", file=sys.stderr)
    sys.exit(1)


# Memory limits regex: positive number followed by optional unit (b, k, m, g, t, p with optional i and b)
MEMORY_REGEX = re.compile(
    r"^(\d+(?:\.\d+)?)\s*(b|k|m|g|t|p|kb|mb|gb|tb|pb|kib|mib|gib|tib|pib)?$",
    re.IGNORECASE,
)

# Standard presets
PRESETS: Dict[str, Dict[str, str]] = {
    "low": {
        "postgres": "512M",
        "mosquitto": "128M",
        "rust-core": "256M",
        "python-api": "512M",
    },
    "default": {
        "postgres": "1.5G",
        "mosquitto": "500M",
        "rust-core": "500M",
        "python-api": "1G",
    },
    "high": {
        "postgres": "4G",
        "mosquitto": "1G",
        "rust-core": "1G",
        "python-api": "2G",
    },
}

UNIT_MULTIPLIERS = {
    "b": 1,
    "k": 1024,
    "kb": 1000,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1000**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1000**3,
    "gib": 1024**3,
    "t": 1024**4,
    "tb": 1000**4,
    "tib": 1024**4,
    "p": 1024**5,
    "pb": 1000**5,
    "pib": 1024**5,
}


def find_compose_file(specified_path: Optional[str] = None) -> Path:
    """Locate docker-compose.yml from specified path or standard locations."""
    if specified_path:
        p = Path(specified_path).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"Specified compose file not found: {specified_path}")
        return p

    cwd = Path.cwd()
    script_dir = Path(__file__).resolve().parent

    candidates = [
        cwd / "docker-compose.yml",
        cwd / "docker-compose.yaml",
        script_dir / "docker-compose.yml",
        script_dir / "docker-compose.yaml",
    ]

    for c in candidates:
        if c.is_file():
            return c.resolve()

    raise FileNotFoundError("Could not find docker-compose.yml in current directory or script directory.")


def validate_memory_limit(limit_str: str) -> bool:
    """Validate that the given string represents a valid, non-zero memory limit."""
    if not isinstance(limit_str, str):
        return False
    match = MEMORY_REGEX.match(limit_str.strip())
    if not match:
        return False
    try:
        val = float(match.group(1))
        return val > 0
    except ValueError:
        return False


def normalize_memory_limit(limit_str: str) -> str:
    """Normalize valid memory string to canonical continuous format without spaces (e.g. '1.5 G' -> '1.5G')."""
    if not isinstance(limit_str, str):
        return str(limit_str)
    match = MEMORY_REGEX.match(limit_str.strip())
    if not match:
        return limit_str.strip()
    unit = match.group(2) or ""
    return f"{match.group(1)}{unit}"


def parse_bytes(limit_str: str) -> Optional[int]:
    """Parse memory string into total bytes for display/comparison."""
    match = MEMORY_REGEX.match(limit_str.strip())
    if not match:
        return None
    val = float(match.group(1))
    unit = (match.group(2) or "b").lower()
    multiplier = UNIT_MULTIPLIERS.get(unit, 1)
    return int(val * multiplier)


def load_compose_doc(file_path: Path) -> dict:
    """Load and parse docker-compose.yml using yaml.safe_load."""
    with open(file_path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    if not isinstance(doc, dict):
        raise ValueError(f"Invalid docker-compose format in {file_path}")
    return doc


def get_all_services_and_limits(file_path: Path) -> Dict[str, Optional[str]]:
    """Return dictionary of service_name -> current memory limit string."""
    doc = load_compose_doc(file_path)
    services = doc.get("services", {})
    result = {}
    for name, s_cfg in services.items():
        if not isinstance(s_cfg, dict):
            continue
        mem = (
            s_cfg.get("deploy", {})
            .get("resources", {})
            .get("limits", {})
            .get("memory")
        )
        result[name] = str(mem) if mem is not None else None
    return result


def update_service_memory_in_text(content: str, target_service: str, new_limit: str) -> Tuple[str, bool]:
    """
    Update memory limit for target_service in content preserving comments, indentation,
    and line structure. Returns (new_content, updated_bool).
    """
    new_limit = normalize_memory_limit(new_limit)
    lines = content.splitlines(keepends=True)
    new_lines = []
    in_services = False
    curr_service = None
    in_deploy = False
    in_resources = False
    in_limits = False
    updated = False

    target_service_found = False
    target_service_line_idx = -1

    for idx, line in enumerate(lines):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        # Track top-level YAML sections
        if indent == 0 and stripped.endswith(":") and not stripped.startswith("#"):
            section_name = stripped[:-1].strip()
            in_services = (section_name == "services")
            curr_service = None
            in_deploy = in_resources = in_limits = False
        elif in_services:
            # Services are indented by 2 spaces
            if indent == 2 and stripped.endswith(":") and not stripped.startswith("#"):
                curr_service = stripped[:-1].strip()
                in_deploy = in_resources = in_limits = False
                if curr_service == target_service:
                    target_service_found = True
                    target_service_line_idx = idx
            elif in_services and curr_service == target_service:
                if indent == 4 and stripped == "deploy:":
                    in_deploy = True
                    in_resources = in_limits = False
                elif in_deploy and indent == 6 and stripped == "resources:":
                    in_resources = True
                    in_limits = False
                elif in_resources and indent == 8 and stripped == "limits:":
                    in_limits = True
                elif in_limits and stripped.startswith("memory:"):
                    # Match pattern: '          memory: <val>  # optional comment'
                    match = re.match(r"^(\s*memory:\s*)(.*?)(\s*#.*)?$", line)
                    if match:
                        trailing_comment = match.group(3) if match.group(3) else ""
                        line = f"{match.group(1)}{new_limit}{trailing_comment}\n"
                        updated = True
                elif indent <= 4 and stripped != "deploy:" and stripped and not stripped.startswith("#"):
                    in_deploy = in_resources = in_limits = False

        new_lines.append(line)

    if not target_service_found:
        raise KeyError(f"Service '{target_service}' was not found in compose file.")

    if not updated:
        # If the service exists but didn't have deploy.resources.limits.memory, insert it
        # Find where to insert deploy stanza inside the service
        insert_idx = target_service_line_idx + 1
        # Insert at the beginning of service properties
        deploy_block = [
            "    deploy:\n",
            "      resources:\n",
            "        limits:\n",
            f"          memory: {new_limit}\n",
        ]
        new_lines = new_lines[:insert_idx] + deploy_block + new_lines[insert_idx:]
        updated = True

    return "".join(new_lines), updated


def modify_service_limit(
    file_path: Path, service: str, new_limit: str, dry_run: bool = False
) -> Tuple[bool, Optional[str], str]:
    """Modify a single service limit and safely write back to file."""
    if not validate_memory_limit(new_limit):
        return False, None, f"Invalid memory limit format: '{new_limit}'. Examples: 500M, 1.5G, 2G"

    new_limit = normalize_memory_limit(new_limit)

    with open(file_path, "r", encoding="utf-8") as f:
        original_content = f.read()

    current_limits = get_all_services_and_limits(file_path)
    if service not in current_limits:
        return False, None, f"Service '{service}' not found. Available services: {', '.join(current_limits.keys())}"

    old_limit = current_limits[service]

    new_content, updated = update_service_memory_in_text(original_content, service, new_limit)
    if not updated:
        return False, old_limit, f"Failed to update memory limit for service '{service}'."

    # Validate resulting YAML syntax and values
    try:
        doc = yaml.safe_load(new_content)
        verified_val = (
            doc.get("services", {})
            .get(service, {})
            .get("deploy", {})
            .get("resources", {})
            .get("limits", {})
            .get("memory")
        )
        if str(verified_val) != new_limit:
            return (
                False,
                old_limit,
                f"YAML validation mismatch: expected '{new_limit}', found '{verified_val}'.",
            )
    except Exception as e:
        return False, old_limit, f"Generated YAML failed validation: {e}"

    if not dry_run:
        temp_path = file_path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        temp_path.replace(file_path)

    return True, old_limit, f"Successfully set '{service}' memory limit to {new_limit}."


def modify_multiple_limits(
    file_path: Path, updates: Dict[str, str], dry_run: bool = False
) -> Tuple[bool, Dict[str, Tuple[Optional[str], str]], str]:
    """Apply multiple memory limit updates atomically."""
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    current_limits = get_all_services_and_limits(file_path)
    report = {}

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
                return (
                    False,
                    {},
                    f"YAML validation mismatch for '{service}': expected '{new_limit}', got '{verified_val}'.",
                )
    except Exception as e:
        return False, {}, f"Generated YAML failed validation: {e}"

    if not dry_run:
        temp_path = file_path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(content)
        temp_path.replace(file_path)

    return True, report, "All limits updated successfully."


def cli_show(file_path: Path) -> int:
    """Display current memory limits for all services."""
    try:
        limits = get_all_services_and_limits(file_path)
    except Exception as e:
        print(f"Error reading compose file: {e}", file=sys.stderr)
        return 1

    print("\n" + "=" * 62)
    print(f"  Docker Compose Memory Limits: {file_path.name}")
    print("=" * 62)
    print(f"  {'Service':<20} | {'Memory Limit':<15} | {'Bytes':<18}")
    print("-" * 62)
    for svc, lim in limits.items():
        if lim is not None:
            b = parse_bytes(lim)
            b_str = f"({b:,} B)" if b is not None else ""
            print(f"  {svc:<20} | {lim:<15} | {b_str:<18}")
        else:
            print(f"  {svc:<20} | {'[Unset]':<15} | {'-':<18}")
    print("=" * 62 + "\n")
    return 0


def cli_verify(file_path: Path) -> int:
    """Validate that current limits adhere to syntax and report values."""
    try:
        limits = get_all_services_and_limits(file_path)
    except Exception as e:
        print(f"Verification FAILED: Could not parse compose file: {e}", file=sys.stderr)
        return 1

    print("\n" + "=" * 62)
    print(f"  Verifying Resource Limits: {file_path.name}")
    print("=" * 62)
    print(f"  {'Service':<20} | {'Configured Limit':<18} | {'Status':<15}")
    print("-" * 62)

    all_valid = True
    for svc, lim in limits.items():
        if lim is None:
            print(f"  {svc:<20} | {'[Unset]':<18} | \033[93mWARNING (Unset)\033[0m")
            all_valid = False
        elif validate_memory_limit(lim):
            print(f"  {svc:<20} | {lim:<18} | \033[92mVALID\033[0m")
        else:
            print(f"  {svc:<20} | {lim:<18} | \033[91mINVALID FORMAT\033[0m")
            all_valid = False

    print("=" * 62)
    if all_valid:
        print("  \033[92mAll memory limits are valid and correctly configured.\033[0m\n")
        return 0
    else:
        print("  \033[91mOne or more services have invalid or missing memory limits.\033[0m\n")
        return 1


def interactive_menu(file_path: Path) -> int:
    """Interactive terminal menu mode."""
    while True:
        print("\n" + "=" * 62)
        print("           TR-369 / USP ACS Resource Limit Setup")
        print("=" * 62)
        print(f"  Target File: {file_path}")
        print("-" * 62)
        print("  1. Show current memory limits")
        print("  2. Modify limit for a specific service")
        print("  3. Apply preset profile (low / default / high)")
        print("  4. Verify current configuration")
        print("  5. Exit")
        print("=" * 62)

        try:
            choice = input("  Select an option [1-5]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break

        if choice == "1":
            cli_show(file_path)
        elif choice == "2":
            limits = get_all_services_and_limits(file_path)
            svc_list = list(limits.keys())
            print("\n  Available services:")
            for i, s in enumerate(svc_list, start=1):
                cur = limits.get(s, "[Unset]")
                print(f"    {i}. {s:<18} (current: {cur})")

            try:
                svc_sel = input(f"  Select service [1-{len(svc_list)}] or name (blank to cancel): ").strip()
                if not svc_sel:
                    continue

                if svc_sel.isdigit() and 1 <= int(svc_sel) <= len(svc_list):
                    target_svc = svc_list[int(svc_sel) - 1]
                elif svc_sel in svc_list:
                    target_svc = svc_sel
                else:
                    print(f"  Invalid service selection: '{svc_sel}'")
                    continue

                new_lim = input(f"  Enter new memory limit for '{target_svc}' (e.g. 500M, 1.5G, 2G): ").strip()
                if not new_lim:
                    print("  Cancelled.")
                    continue

                success, old_lim, msg = modify_service_limit(file_path, target_svc, new_lim)
                if success:
                    print(f"\n  \033[92mSuccess:\033[0m {target_svc}: {old_lim} -> {new_lim}")
                else:
                    print(f"\n  \033[91mError:\033[0m {msg}")
            except (EOFError, KeyboardInterrupt):
                print("\nOperation cancelled.")
                continue

        elif choice == "3":
            print("\n  Available Presets:")
            for p_name, p_vals in PRESETS.items():
                print(f"    - {p_name:<10}: {', '.join(f'{k}={v}' for k, v in p_vals.items())}")

            try:
                preset_sel = input("  Select preset (low / default / high) or blank to cancel: ").strip().lower()
                if not preset_sel:
                    continue
                if preset_sel not in PRESETS:
                    print(f"  Unknown preset: '{preset_sel}'")
                    continue

                success, report, msg = modify_multiple_limits(file_path, PRESETS[preset_sel])
                if success:
                    print(f"\n  \033[92mSuccess:\033[0m Applied preset '{preset_sel}':")
                    for svc, (old_v, new_v) in report.items():
                        print(f"    {svc:<18}: {old_v} -> {new_v}")
                else:
                    print(f"\n  \033[91mError:\033[0m {msg}")
            except (EOFError, KeyboardInterrupt):
                print("\nOperation cancelled.")
                continue

        elif choice == "4":
            cli_verify(file_path)
        elif choice == "5":
            print("Exiting.")
            break
        else:
            print("  Invalid option. Please choose between 1 and 5.")

    return 0


def run_tests() -> int:
    """Run internal test suite covering all functionality."""
    import tempfile
    import unittest

    class TestConfigureLimits(unittest.TestCase):
        def test_validate_memory_limit_valid(self):
            valid_cases = ["500M", "1.5G", "1G", "512m", "256MiB", "1024", "2G", "64MB", "0.5G"]
            for v in valid_cases:
                self.assertTrue(validate_memory_limit(v), f"Expected '{v}' to be valid")

        def test_validate_memory_limit_invalid(self):
            invalid_cases = ["0", "0M", "abc", "-1G", "", "   ", None, "1.2.3G"]
            for v in invalid_cases:
                self.assertFalse(validate_memory_limit(v), f"Expected '{v}' to be invalid")

        def test_parse_bytes(self):
            self.assertEqual(parse_bytes("1G"), 1024**3)
            self.assertEqual(parse_bytes("500M"), 500 * 1024**2)
            self.assertEqual(parse_bytes("1024"), 1024)
            self.assertIsNone(parse_bytes("invalid"))

        def test_comment_and_indentation_preservation(self):
            sample_yaml = (
                "# Top comment\n"
                "version: '3.8'\n\n"
                "services:\n"
                "  # Service comment\n"
                "  postgres:\n"
                "    image: postgres:15-alpine\n"
                "    deploy:\n"
                "      resources:\n"
                "        limits:\n"
                "          memory: 1.5G  # Inline comment\n"
            )
            new_text, updated = update_service_memory_in_text(sample_yaml, "postgres", "3G")
            self.assertTrue(updated)
            self.assertIn("# Top comment", new_text)
            self.assertIn("# Service comment", new_text)
            self.assertIn("# Inline comment", new_text)
            self.assertIn("memory: 3G", new_text)

        def test_modify_service_limit_workflow(self):
            with tempfile.TemporaryDirectory() as tmpdir:
                compose_f = Path(tmpdir) / "docker-compose.yml"
                compose_f.write_text(
                    "version: '3.8'\nservices:\n  postgres:\n    deploy:\n      resources:\n        limits:\n          memory: 1.5G\n"
                )
                ok, old, msg = modify_service_limit(compose_f, "postgres", "2G")
                self.assertTrue(ok)
                self.assertEqual(old, "1.5G")
                limits = get_all_services_and_limits(compose_f)
                self.assertEqual(limits["postgres"], "2G")

        def test_modify_service_limit_dry_run(self):
            with tempfile.TemporaryDirectory() as tmpdir:
                compose_f = Path(tmpdir) / "docker-compose.yml"
                compose_f.write_text(
                    "version: '3.8'\nservices:\n  postgres:\n    deploy:\n      resources:\n        limits:\n          memory: 1.5G\n"
                )
                ok, old, msg = modify_service_limit(compose_f, "postgres", "2G", dry_run=True)
                self.assertTrue(ok)
                limits = get_all_services_and_limits(compose_f)
                self.assertEqual(limits["postgres"], "1.5G")

        def test_modify_service_limit_invalid_service(self):
            with tempfile.TemporaryDirectory() as tmpdir:
                compose_f = Path(tmpdir) / "docker-compose.yml"
                compose_f.write_text(
                    "version: '3.8'\nservices:\n  postgres:\n    deploy:\n      resources:\n        limits:\n          memory: 1.5G\n"
                )
                ok, old, msg = modify_service_limit(compose_f, "nonexistent", "2G")
                self.assertFalse(ok)
                self.assertIn("not found", msg.lower())

        def test_modify_service_limit_invalid_value(self):
            with tempfile.TemporaryDirectory() as tmpdir:
                compose_f = Path(tmpdir) / "docker-compose.yml"
                compose_f.write_text(
                    "version: '3.8'\nservices:\n  postgres:\n    deploy:\n      resources:\n        limits:\n          memory: 1.5G\n"
                )
                ok, old, msg = modify_service_limit(compose_f, "postgres", "invalid_limit")
                self.assertFalse(ok)
                self.assertIn("invalid memory limit", msg.lower())

        def test_presets_application(self):
            with tempfile.TemporaryDirectory() as tmpdir:
                compose_f = Path(tmpdir) / "docker-compose.yml"
                compose_f.write_text(
                    "version: '3.8'\nservices:\n"
                    "  postgres:\n    deploy:\n      resources:\n        limits:\n          memory: 1G\n"
                    "  mosquitto:\n    deploy:\n      resources:\n        limits:\n          memory: 1G\n"
                    "  rust-core:\n    deploy:\n      resources:\n        limits:\n          memory: 1G\n"
                    "  python-api:\n    deploy:\n      resources:\n        limits:\n          memory: 1G\n"
                )
                ok, report, msg = modify_multiple_limits(compose_f, PRESETS["low"])
                self.assertTrue(ok)
                limits = get_all_services_and_limits(compose_f)
                self.assertEqual(limits["postgres"], "512M")
                self.assertEqual(limits["mosquitto"], "128M")
                self.assertEqual(limits["rust-core"], "256M")
                self.assertEqual(limits["python-api"], "512M")

        def test_space_separated_memory_limit_handling(self):
            # 1. Helper function verification
            self.assertEqual(normalize_memory_limit("1.5 G"), "1.5G")
            self.assertEqual(normalize_memory_limit("  500   MB  "), "500MB")
            self.assertEqual(normalize_memory_limit("2G"), "2G")
            self.assertEqual(normalize_memory_limit("1024"), "1024")

            with tempfile.TemporaryDirectory() as tmpdir:
                compose_f = Path(tmpdir) / "docker-compose.yml"
                compose_f.write_text(
                    "version: '3.8'\n"
                    "services:\n"
                    "  postgres:\n"
                    "    deploy:\n"
                    "      resources:\n"
                    "        limits:\n"
                    "          memory: 1.5G  # initial comment\n"
                    "  mosquitto:\n"
                    "    deploy:\n"
                    "      resources:\n"
                    "        limits:\n"
                    "          memory: 500M\n"
                )

                # 2. Mutation 1: Spaced input "1.5 G" -> normalized to "1.5G"
                ok1, old1, msg1 = modify_service_limit(compose_f, "postgres", "1.5 G")
                self.assertTrue(ok1, f"Failed setting '1.5 G': {msg1}")
                self.assertEqual(old1, "1.5G")
                limits = get_all_services_and_limits(compose_f)
                self.assertEqual(limits["postgres"], "1.5G")

                # Verify comment was preserved
                content = compose_f.read_text()
                self.assertIn("memory: 1.5G  # initial comment", content)

                # 3. Mutation 2: Subsequent mutation to "2G" without suffix accumulation ("2G G")
                ok2, old2, msg2 = modify_service_limit(compose_f, "postgres", "2G")
                self.assertTrue(ok2, f"Failed setting '2G' after '1.5 G': {msg2}")
                self.assertEqual(old2, "1.5G")
                limits = get_all_services_and_limits(compose_f)
                self.assertEqual(limits["postgres"], "2G")

                # Verify comment was preserved and no suffix accumulation
                content = compose_f.read_text()
                self.assertIn("memory: 2G  # initial comment", content)
                self.assertNotIn("2G G", content)

                # 4. Existing spaced value in file replaced cleanly
                spaced_yaml = (
                    "version: '3.8'\n"
                    "services:\n"
                    "  postgres:\n"
                    "    deploy:\n"
                    "      resources:\n"
                    "        limits:\n"
                    "          memory: 1.5 G  # pre-existing spaced comment\n"
                    "  mosquitto:\n"
                    "    deploy:\n"
                    "      resources:\n"
                    "        limits:\n"
                    "          memory: 500M\n"
                )
                compose_f.write_text(spaced_yaml)
                ok3, old3, msg3 = modify_service_limit(compose_f, "postgres", "3G")
                self.assertTrue(ok3, f"Failed updating existing spaced limit: {msg3}")
                limits = get_all_services_and_limits(compose_f)
                self.assertEqual(limits["postgres"], "3G")
                content = compose_f.read_text()
                self.assertIn("memory: 3G  # pre-existing spaced comment", content)

                # 5. Spaced limits in modify_multiple_limits
                multi_updates = {"postgres": "2.5 G", "mosquitto": "256 M"}
                ok4, report, msg4 = modify_multiple_limits(compose_f, multi_updates)
                self.assertTrue(ok4, f"Failed modify_multiple_limits with spaces: {msg4}")
                limits = get_all_services_and_limits(compose_f)
                self.assertEqual(limits["postgres"], "2.5G")
                self.assertEqual(limits["mosquitto"], "256M")

    suite = unittest.TestLoader().loadTestsFromTestCase(TestConfigureLimits)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="TR-369/USP ACS Docker Compose Resource Limit Configuration Tool (R3)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  ./configure_limits.py                               # Launch interactive menu
  ./configure_limits.py --show                        # Display current limits
  ./configure_limits.py --service postgres --limit 1.5G # Update postgres limit
  ./configure_limits.py --preset default              # Apply default profile
  ./configure_limits.py --verify                      # Validate limit syntax
        """,
    )

    parser.add_argument(
        "--compose-file",
        type=str,
        default=None,
        help="Path to docker-compose.yml file (defaults to auto-detect)",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Display current memory limits for all services",
    )
    parser.add_argument(
        "--service",
        type=str,
        help="Name of the service to modify (e.g. postgres, mosquitto, rust-core, python-api)",
    )
    parser.add_argument(
        "--limit",
        type=str,
        help="New memory limit value (e.g. 500M, 1.5G, 2G)",
    )
    parser.add_argument(
        "--preset",
        type=str,
        choices=list(PRESETS.keys()),
        help="Apply a predefined memory profile (low, default, high)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Validate that current memory limits conform to expected syntax",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate modifications without writing changes to file",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run built-in automated test suite",
    )

    args = parser.parse_args()

    if args.test:
        return run_tests()

    try:
        compose_path = find_compose_file(args.compose_file)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Check if any CLI option was provided
    has_cli_option = bool(
        args.show or args.service or args.limit or args.preset or args.verify
    )

    if not has_cli_option:
        # Dual mode a): Interactive terminal menu
        return interactive_menu(compose_path)

    # Dual mode b): Non-interactive CLI options
    if args.show:
        return cli_show(compose_path)

    if args.verify:
        return cli_verify(compose_path)

    if args.preset:
        preset_values = PRESETS[args.preset]
        success, report, msg = modify_multiple_limits(
            compose_path, preset_values, dry_run=args.dry_run
        )
        if success:
            print(f"Preset '{args.preset}' applied successfully" + (" (DRY RUN):" if args.dry_run else ":"))
            for svc, (old_v, new_v) in report.items():
                print(f"  - {svc:<18}: {old_v} -> {new_v}")
            return 0
        else:
            print(f"Error applying preset '{args.preset}': {msg}", file=sys.stderr)
            return 1

    if args.service or args.limit:
        if not (args.service and args.limit):
            print("Error: Both --service and --limit must be specified together.", file=sys.stderr)
            return 1

        success, old_lim, msg = modify_service_limit(
            compose_path, args.service, args.limit, dry_run=args.dry_run
        )
        if success:
            print(f"{'Simulated update' if args.dry_run else 'Updated'} '{args.service}' memory limit: {old_lim} -> {args.limit}")
            return 0
        else:
            print(f"Error: {msg}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
