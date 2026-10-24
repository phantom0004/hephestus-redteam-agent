"""VM Snapshot Management via VBoxManage subprocess calls."""

import subprocess
import sys
from datetime import datetime, timezone


class VMManager:
    """Manages VirtualBox VM snapshots via VBoxManage CLI."""

    def __init__(self, vboxmanage_path: str = "VBoxManage"):
        self.vboxmanage = vboxmanage_path

    def _run(self, args: list[str]) -> dict[str, str]:
        """Run a VBoxManage command and return result."""
        cmd = [self.vboxmanage] + args
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            return {
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
                "exit_code": proc.returncode,
            }
        except FileNotFoundError:
            return {
                "stdout": "",
                "stderr": f"VBoxManage not found at: {self.vboxmanage}",
                "exit_code": -1,
            }
        except subprocess.TimeoutExpired:
            return {
                "stdout": "",
                "stderr": "VBoxManage command timed out",
                "exit_code": 124,
            }

    def get_vm_state(self, vm_name: str) -> str | None:
        """Get the current state of a VM."""
        result = self._run(["showvminfo", vm_name, "--machinereadable"])
        if result["exit_code"] != 0:
            return None
        for line in result["stdout"].splitlines():
            if line.startswith("VMState="):
                return line.split("=", 1)[1].strip('"')
        return None

    def create_snapshot(
        self, vm_name: str, snapshot_name: str = None
    ) -> dict[str, str]:
        """Create a snapshot of the VM. VM should be stopped or saved."""
        state = self.get_vm_state(vm_name)
        if state not in ("poweroff", "saved", "aborted"):
            return {
                "success": False,
                "message": f"VM must be stopped before snapshot (current state: {state})",
            }

        if not snapshot_name:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            snapshot_name = f"hephestus_snap_{ts}"

        result = self._run(["snapshot", vm_name, "take", snapshot_name])
        if result["exit_code"] == 0:
            return {
                "success": True,
                "message": f"Snapshot '{snapshot_name}' created",
                "name": snapshot_name,
            }
        return {
            "success": False,
            "message": result["stderr"] or "Failed to create snapshot",
        }

    def restore_snapshot(self, vm_name: str, snapshot_name: str) -> dict[str, str]:
        """Restore a VM to a specific snapshot."""
        state = self.get_vm_state(vm_name)
        if state not in ("poweroff", "saved", "aborted"):
            return {
                "success": False,
                "message": "Stop or save the VM before restoring a snapshot",
            }
        snapshots = self.list_snapshots(vm_name)
        if snapshot_name not in [s["name"] for s in snapshots]:
            return {
                "success": False,
                "message": f"Snapshot '{snapshot_name}' not found",
            }

        result = self._run(["snapshot", vm_name, "restore", snapshot_name])
        if result["exit_code"] == 0:
            return {
                "success": True,
                "message": f"Restored to snapshot '{snapshot_name}'",
            }
        return {
            "success": False,
            "message": result["stderr"] or "Failed to restore snapshot",
        }

    def list_snapshots(self, vm_name: str) -> list[dict[str, str]]:
        """List all snapshots for a VM."""
        result = self._run(["snapshot", vm_name, "list", "--machinereadable"])
        if result["exit_code"] != 0:
            return []

        snapshots = []
        current_name = None
        current_uuid = None
        for line in result["stdout"].splitlines():
            if line.startswith("SnapshotName"):
                current_name = line.split("=", 1)[1].strip('"')
            elif line.startswith("SnapshotUUID"):
                current_uuid = line.split("=", 1)[1].strip('"')
                if current_name:
                    snapshots.append({"name": current_name, "uuid": current_uuid or ""})
                    current_name = None
                    current_uuid = None
        return snapshots


def main():
    """CLI entry point for VM snapshot management."""
    if len(sys.argv) < 3:
        print("Usage:")
        print(
            "  python -m hephestus.tools.vm_manager snapshot create <vm_name> [snapshot_name]"
        )
        print(
            "  python -m hephestus.tools.vm_manager snapshot restore <vm_name> <snapshot_name>"
        )
        print("  python -m hephestus.tools.vm_manager snapshot list <vm_name>")
        sys.exit(1)

    manager = VMManager()
    action = sys.argv[1]
    subaction = sys.argv[2] if len(sys.argv) > 2 else None

    if action == "snapshot":
        if subaction == "create" and len(sys.argv) >= 4:
            vm_name = sys.argv[3]
            snap_name = sys.argv[4] if len(sys.argv) > 4 else None
            result = manager.create_snapshot(vm_name, snap_name)
            print(result["message"])
            sys.exit(0 if result["success"] else 1)
        elif subaction == "restore" and len(sys.argv) >= 5:
            vm_name = sys.argv[3]
            snap_name = sys.argv[4]
            result = manager.restore_snapshot(vm_name, snap_name)
            print(result["message"])
            sys.exit(0 if result["success"] else 1)
        elif subaction == "list" and len(sys.argv) >= 4:
            vm_name = sys.argv[3]
            snapshots = manager.list_snapshots(vm_name)
            if not snapshots:
                print(f"No snapshots found for VM '{vm_name}'")
            else:
                for snap in snapshots:
                    print(f"  {snap['name']}  (UUID: {snap['uuid']})")
            sys.exit(0)

    print("Invalid command. Use --help for usage.")
    sys.exit(1)


if __name__ == "__main__":
    main()
