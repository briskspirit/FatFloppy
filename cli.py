import datetime
import os
import struct

from fat import FAT12FileSystem


def create_image_data(params):
    """Create a new disk image from parameters."""
    sector_size = params['sector_size']
    sectors_per_track = params['sectors_per_track']
    num_tracks = params['num_tracks']
    num_heads = params['num_heads']
    num_fats = params['num_fats']
    root_entries = params['root_entries']
    sectors_per_cluster = params['sectors_per_cluster']
    media_descriptor = params['media_descriptor']
    reserved_sectors = params['reserved_sectors']
    oem_id = params['oem_id']

    total_sectors = num_tracks * sectors_per_track * num_heads
    root_dir_sectors = (root_entries * 32 + sector_size - 1) // sector_size
    sectors_per_fat = FloppyBPB.calculate_sectors_per_fat(total_sectors, reserved_sectors, num_fats,
                                                           root_dir_sectors, sectors_per_cluster, sector_size)

    boot_sector = bytearray(512)
    boot_sector[0:3] = b'\xEB\xFE\x90'
    boot_sector[3:11] = oem_id.encode('cp437').ljust(8)
    struct.pack_into('<H', boot_sector, 0x00B, sector_size)
    struct.pack_into('<B', boot_sector, 0x00D, sectors_per_cluster)
    struct.pack_into('<H', boot_sector, 0x00E, reserved_sectors)
    struct.pack_into('<B', boot_sector, 0x010, num_fats)
    struct.pack_into('<H', boot_sector, 0x011, root_entries)
    if total_sectors < 65536:
        struct.pack_into('<H', boot_sector, 0x013, total_sectors)
        struct.pack_into('<I', boot_sector, 0x020, 0)
    else:
        struct.pack_into('<H', boot_sector, 0x013, 0)
        struct.pack_into('<I', boot_sector, 0x020, total_sectors)
    struct.pack_into('<B', boot_sector, 0x015, media_descriptor)
    struct.pack_into('<H', boot_sector, 0x016, sectors_per_fat)
    struct.pack_into('<H', boot_sector, 0x018, sectors_per_track)
    struct.pack_into('<H', boot_sector, 0x01A, num_heads)
    struct.pack_into('<I', boot_sector, 0x01C, 0)
    struct.pack_into('<B', boot_sector, 0x024, 0)
    struct.pack_into('<B', boot_sector, 0x025, 0)
    struct.pack_into('<B', boot_sector, 0x026, 0x29)
    struct.pack_into('<I', boot_sector, 0x027, 0)
    boot_sector[0x02B:0x036] = 'NO NAME '.encode('cp437').ljust(11)
    boot_sector[0x036:0x03E] = 'FAT12   '.encode('cp437').ljust(8)
    struct.pack_into('<H', boot_sector, 0x1FE, 0xAA55)

    image_size = total_sectors * sector_size
    image_data = bytearray(image_size)
    image_data[0:512] = boot_sector
    return image_data

def get_timestamp():
    user_input = input("Enter timestamp (YYYY-MM-DD HH:MM:SS) or press Enter for current time: ")
    if user_input.strip() == "":
        return datetime.datetime.now()
    try:
        return datetime.datetime.strptime(user_input, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        print("Invalid timestamp format. Using current time.")
        return datetime.datetime.now()

def run_cui():
    from diskmanager import FloppyDiskManager, ImageFileManager
    from floppybpb import FloppyBPB

    floppy_variants = {
        "1.44MB 3.5\" HD": {
            'sector_size': 512, 'sectors_per_track': 18, 'num_tracks': 80, 'num_heads': 2,
            'num_fats': 2, 'root_entries': 224, 'sectors_per_cluster': 1, 'media_descriptor': 0xF0,
            'reserved_sectors': 1, 'oem_id': "MSDOS5.0"
        },
        "720KB 3.5\" DD": {
            'sector_size': 512, 'sectors_per_track': 9, 'num_tracks': 80, 'num_heads': 2,
            'num_fats': 2, 'root_entries': 112, 'sectors_per_cluster': 2, 'media_descriptor': 0xF9,
            'reserved_sectors': 1, 'oem_id': "MSDOS5.0"
        },
        "360KB 5.25\" DD": {
            'sector_size': 512, 'sectors_per_track': 9, 'num_tracks': 40, 'num_heads': 2,
            'num_fats': 2, 'root_entries': 112, 'sectors_per_cluster': 2, 'media_descriptor': 0xFD,
            'reserved_sectors': 1, 'oem_id': "MSDOS5.0"
        },
        "160KB 5.25\" DD": {
            'sector_size': 512, 'sectors_per_track': 8, 'num_tracks': 40, 'num_heads': 1,
            'num_fats': 2, 'root_entries': 64, 'sectors_per_cluster': 1, 'media_descriptor': 0xFE,
            'reserved_sectors': 1, 'oem_id': "MSDOS5.0"
        }
    }

    print("Choose operation mode:")
    print("1. Work with image file")
    print("2. Work with floppy disk")
    mode_choice = input("Enter choice: ")
    if mode_choice == '1':
        file_path = input("Path to image file (.ima .img etc.): ")
        if os.path.exists(file_path):
            disk_manager = ImageFileManager(file_path)
            is_new_image = False
        else:
            print(f"File {file_path} does not exist. Create a new image? (y/n)")
            if input().lower() != 'y':
                print("Exiting.")
                return
            print("Select a floppy variant or enter custom parameters:")
            for i, variant in enumerate(floppy_variants.keys(), 1):
                print(f"{i}. {variant}")
            print(f"{len(floppy_variants)+1}. Custom")
            try:
                choice = int(input("Enter choice: "))
                if 1 <= choice <= len(floppy_variants):
                    params = floppy_variants[list(floppy_variants.keys())[choice-1]]
                else:
                    params = {}
                    params['sector_size'] = int(input("Sector size (e.g., 512): "))
                    params['sectors_per_track'] = int(input("Sectors per track: "))
                    params['num_tracks'] = int(input("Number of tracks: "))
                    params['num_heads'] = int(input("Number of heads: "))
                    params['num_fats'] = int(input("Number of FATs: "))
                    params['root_entries'] = int(input("Root directory entries: "))
                    params['sectors_per_cluster'] = int(input("Sectors per cluster: "))
                    params['media_descriptor'] = int(input("Media descriptor (hex, e.g., 0xF0): "), 16)
                    params['reserved_sectors'] = int(input("Reserved sectors (default 1): ") or "1")
                    params['oem_id'] = input("OEM ID (default 'MSDOS5.0'): ") or "MSDOS5.0"
                image_data = create_image_data(params)
                disk_manager = ImageFileManager(file_path, image_data)
                is_new_image = True
            except ValueError:
                print("Invalid input. Exiting.")
                return
    elif mode_choice == '2':
        device_name = input("Enter device name (e.g., COM3): ") or None
        format_name = input("Enter disk format (e.g., ibm.1440): ") or 'ibm.scan'
        try:
            disk_manager = FloppyDiskManager(device_name, format_name=format_name)
            is_new_image = False
        except Exception as e:
            print(f"Failed to initialize floppy disk: {e}")
            return
    else:
        print("Invalid choice. Exiting.")
        return

    floppy_bpb = FloppyBPB(disk_manager)
    params = floppy_bpb.get_fat12_params()
    fs = FAT12FileSystem(disk_manager, params)
    if mode_choice == '1' and is_new_image:
        fs.initialize_fats()
    print("Disk loaded successfully.")

    while True:
        print("\nOptions:")
        print("1. List files")
        print("2. Extract file")
        print("3. Create directory")
        print("4. Delete directory/file")
        print("5. Insert file")
        print("6. Print Boot Sector Information")
        print("9. Exit")
        choice = input("Enter your choice: ")
        if choice == '1':
            for file in fs.list_files():
                dt_str = file['datetime'].strftime("%Y-%m-%d %H:%M:%S")
                attr_str = file['attributes']
                if file['is_dir']:
                    print(f"<DIR> {file['name']} {dt_str} {attr_str}")
                else:
                    print(f"      {file['name']} ({file['size']} bytes) {dt_str} {attr_str}")
        elif choice == '2':
            path = input("Enter file path to extract (e.g., /DIR1/FILE.TXT): ")
            try:
                file_data = fs.extract_file(path)
                file_name = path.split('/')[-1]
                with open(file_name, 'wb') as f:
                    f.write(file_data)
                print(f"Extracted {file_name}")
            except ValueError as e:
                print(e)
        elif choice == '3':
            parent_path = input("Enter parent directory path (e.g., / or /DIR1): ")
            new_dir_name = input("Enter new directory name (8.3 format): ")
            dt = get_timestamp()
            try:
                fs.create_directory(parent_path, new_dir_name, dt)
                print("Directory created successfully")
            except ValueError as e:
                print(e)
        elif choice == '4':
            path = input("Enter path to delete (e.g., /FILE.TXT or /DIR1): ")
            try:
                fs.delete_item(path)
                print("Deleted successfully")
            except ValueError as e:
                print(e)
        elif choice == '5':
            parent_path = input("Enter parent directory path (e.g., / or /DIR1): ")
            file_path = input("Enter path to file to insert: ")
            new_file_name = input("Enter file name in image (8.3 format): ")
            try:
                with open(file_path, 'rb') as f:
                    file_data = bytearray(f.read())
                dt = get_timestamp()
                fs.insert_file(parent_path, new_file_name, file_data, dt)
                print("File inserted successfully")
            except (ValueError, FileNotFoundError) as e:
                print(e)
        elif choice == '6':
            print("**Boot Sector Information**")
            print(f"Jump Code: {floppy_bpb.jump_code.hex()}")
            print(f"OEM Identifier: {floppy_bpb.oem_id}")
            print(f"Bytes Per Sector: {floppy_bpb.bytes_per_sector}")
            print(f"Sectors Per Cluster: {floppy_bpb.sectors_per_cluster}")
            print(f"Reserved Sectors: {floppy_bpb.reserved_sectors}")
            print(f"Num FATs: {floppy_bpb.num_fats}")
            print(f"Root Entries: {floppy_bpb.root_entries}")
            print(f"Total Sectors: {floppy_bpb.total_sectors}")
            print(f"Media Descriptor: {hex(floppy_bpb.media_descriptor)}")
            print(f"Sectors Per FAT: {floppy_bpb.sectors_per_fat}")
            print(f"Sectors Per Track: {floppy_bpb.sectors_per_track}")
            print(f"Num Heads: {floppy_bpb.num_heads}")
            print(f"Hidden Sectors: {floppy_bpb.hidden_sectors}")
            print(f"Total Sectors Large: {floppy_bpb.total_sectors_large}")
            print(f"Drive Number: {floppy_bpb.drive_number}")
            print(f"Flags: {floppy_bpb.flags}")
            print(f"Extended Boot Signature: {floppy_bpb.signature_ext}")
            print(f"Volume Serial: {floppy_bpb.volume_serial}")
            print(f"Volume Label: {floppy_bpb.volume_label}")
            print(f"File System Type: {floppy_bpb.fs_type}")
            print(f"Disk Type: {floppy_bpb.get_disk_type()}")
            print(f"Total Size: {floppy_bpb.total_sectors * floppy_bpb.bytes_per_sector} bytes")
            print(f"Bootstrap Code (first 10 bytes): {floppy_bpb.bootstrap_code[:10].hex()}")
            print(f"Signature: {floppy_bpb.signature:04x}")
        elif choice == '9':
            disk_manager.flush()
            print("Changes saved. Exiting.")
            break
        else:
            print("Invalid choice")

if __name__ == "__main__":
    run_cui()
