# tests/resources/generate_test_files.py
# (Run once to create test files if needed)
import os

RESOURCE_DIR = os.path.dirname(__file__)

# --- Create test_file.txt ---
txt_path = os.path.join(RESOURCE_DIR, "TEST.TXT")
if not os.path.exists(txt_path):
    with open(txt_path, "w") as f:
        f.write("Hello, floppy world!")
    print(f"Created {txt_path}")

# --- Create pattern_file.bin ---
bin_path = os.path.join(RESOURCE_DIR, "PATTERN.BIN")
if not os.path.exists(bin_path):
    pattern = b'\xAA\xBB\xCC\xDD'
    file_size = 4096
    with open(bin_path, "wb") as f:
        for _ in range(file_size // len(pattern)):
            f.write(pattern)
    print(f"Created {bin_path}")

# --- Create empty_1.44mb.img (placeholder - use mkfs.fat or similar) ---
# On Linux/macOS: dd if=/dev/zero of=empty_1.44mb.img bs=1k count=1440
#                 mkfs.fat -F 12 empty_1.44mb.img
img_path = os.path.join(RESOURCE_DIR, "empty_1.44mb.img")
if not os.path.exists(img_path):
    print(f"Warning: Placeholder for {img_path}. Please create a 1.44MB FAT12 formatted image.")
    # Optionally create a zeroed file as a placeholder
    # file_size = 1440 * 1024
    # with open(img_path, "wb") as f:
    #     f.write(b'\x00' * file_size)

# --- Create populated_1.44mb.img (placeholder - manual creation needed) ---
pop_img_path = os.path.join(RESOURCE_DIR, "populated_1.44mb.img")
if not os.path.exists(pop_img_path):
     print(f"Warning: Placeholder for {pop_img_path}. Please create from empty, mount, add files/dirs.")

print("Test file generation check complete.")
