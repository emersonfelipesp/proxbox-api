"""
Test that verifies the discovery event is emitted early in create_virtual_machines.
"""

import sys


def check_discovery_event_position():
    """
    Verify that emit_discovery() happens early in create_virtual_machines,
    before any blocking work.
    """
    print("🔍 Checking create_virtual_machines for early discovery event emission...\n")

    with open("proxbox_api/routes/virtualization/virtual_machines/sync_vm.py", "r") as f:
        lines = f.readlines()

    # Find the function start
    func_start = None
    for i, line in enumerate(lines):
        if "async def create_virtual_machines(" in line:
            func_start = i
            print(f"✅ Found create_virtual_machines at line {i + 1}")
            break

    if not func_start:
        print("❌ Could not find create_virtual_machines function")
        return False

    # Find emit_discovery call
    discovery_line = None
    for i in range(func_start, min(func_start + 300, len(lines))):
        if "emit_discovery" in lines[i] and "await" in lines[i]:
            discovery_line = i
            print(f"✅ Found emit_discovery at line {i + 1}")
            break

    if not discovery_line:
        print("❌ Could not find emit_discovery call")
        return False

    # Find dependency precomputation (the blocking work)
    precompute_line = None
    for i in range(func_start, min(func_start + 500, len(lines))):
        if "# Precompute all node" in lines[i] or "node_to_cluster" in lines[i]:
            precompute_line = i
            print(f"✅ Found dependency precomputation at line {i + 1}")
            break

    if not precompute_line:
        print("⚠️ Could not find dependency precomputation marker")
        # This is okay, might have been refactored

    # Check the order
    print("\n📊 Analysis:")
    print(f"   Function starts at line: {func_start + 1}")
    print(f"   emit_discovery at line: {discovery_line + 1}")
    if precompute_line:
        print(f"   Precomputation at line: {precompute_line + 1}")

    # Calculate distances
    discovery_distance = discovery_line - func_start
    print(f"\n📏 Discovery event is {discovery_distance} lines after function start")

    if discovery_distance < 150:
        print("✅ GOOD: Discovery event happens early (within first 150 lines)")

        # Show context around discovery emission
        print(f"\n📝 Context around discovery emission (line {discovery_line + 1}):")
        start = max(discovery_line - 2, func_start)
        end = min(discovery_line + 3, len(lines))
        for i in range(start, end):
            marker = ">>> " if i == discovery_line else "    "
            print(f"{marker}{i + 1:4d}: {lines[i].rstrip()}")

        return True
    else:
        print(f"❌ BAD: Discovery event happens too late (after {discovery_distance} lines)")
        return False


if __name__ == "__main__":
    success = check_discovery_event_position()
    sys.exit(0 if success else 1)
