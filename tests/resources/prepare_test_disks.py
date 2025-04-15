# tests/resources/prepare_test_disks.py
import subprocess
import os
import sys

# --- Configuration ---
GW_EXECUTABLE = "gw"  # Make sure 'gw' is in your PATH or provide full path
MASTER_IMAGE_144 = "path/to/your/master_1.44mb.img" # ** CHANGE THIS **
MASTER_IMAGE_360 = "path/to/your/master_360kb.img" # ** CHANGE THIS **
DRIVE_A_FORMAT = "ibm.mfm" # Corresponds to 1.44MB default name in gw diskdefs
DRIVE_B_FORMAT = "ibm.mfm" # Corresponds to 360KB default name in gw diskdefs (or ibm.mfm for 360k on 1.2 drive?)
                            # Check gw diskdefs for the exact name matching 360KB geometry
                            # Might need "ibm.mfm,secs=9,cyls=40" or similar if no specific name

# --- Check Images Exist ---
if not os.path.exists(MASTER_IMAGE_144):
    print(f"ERROR: Master image not found: {MASTER_IMAGE_144}")
    sys.exit(1)
if not os.path.exists(MASTER_IMAGE_360):
    print(f"ERROR: Master image not found: {MASTER_IMAGE_360}")
    sys.exit(1)

# --- Write Commands ---
# Adjust retries, device as needed
commands = [
    # Write 1.44MB image to Drive A
    [GW_EXECUTABLE, "write", "--drive=A", f"--format={DRIVE_A_FORMAT}", MASTER_IMAGE_144, "--retries=2"],
    # Write 360KB image to Drive B
    [GW_EXECUTABLE, "write", "--drive=B", f"--format={DRIVE_B_FORMAT}", MASTER_IMAGE_360, "--retries=2"]
]

# --- Execute ---
print("Preparing test disks. Ensure Greaseweazle is connected.")
print("Drive A (3.5\") requires a 1.44MB floppy.")
print("Drive B (5.25\") requires a 360KB floppy.")
input("Press Enter to continue...")

for cmd in commands:
    print(f"\nExecuting: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        print("Success:")
        # print(result.stdout) # Optional: show gw output
        if result.stderr:
            print("Stderr:")
            print(result.stderr)
    except subprocess.CalledProcessError as e:
        print(f"ERROR executing command: {' '.join(cmd)}")
        print(f"Return Code: {e.returncode}")
        print("Stdout:")
        print(e.stdout)
        print("Stderr:")
        print(e.stderr)
        print("\nDisk preparation failed. Aborting.")
        sys.exit(1)
    except FileNotFoundError:
        print(f"ERROR: '{GW_EXECUTABLE}' command not found. Is Greaseweazle installed and in PATH?")
        sys.exit(1)

print("\nDisk preparation complete.")
