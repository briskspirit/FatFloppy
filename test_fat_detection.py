import math

from floppy_formats import FLOPPY_FORMATS

def detect_fat12_params(image_data):
    image_size = len(image_data)
    possible_sector_sizes = sorted({fmt['sector_size'] for fmt in FLOPPY_FORMATS})
    print(f"Possible sector sizes: {possible_sector_sizes}")

    # Step 1: Identify sector size and matching formats
    candidates = []
    for sector_size in possible_sector_sizes:
        if image_size % sector_size != 0:
            continue
        total_sectors = image_size // sector_size
        matching_formats = [
            fmt for fmt in FLOPPY_FORMATS
            if fmt['sector_size'] == sector_size and fmt['total_sectors'] == total_sectors
        ]
        if matching_formats:
            candidates.append((sector_size, total_sectors, matching_formats))

    if not candidates:
        print("No matching formats found.")
        return None

    # Step 2: Process each candidate
    for sector_size, total_sectors, formats in candidates:
        # Search for FAT signatures
        fat_offsets = []
        for sector in range(min(20, total_sectors)):
            offset = sector * sector_size
            if offset + 3 > image_size:
                break
            mdb = image_data[offset]
            if (mdb & 0xF0) == 0xF0 and image_data[offset:offset + 3] == bytes([mdb, 0xFF, 0xFF]):
                fat_offsets.append(offset)

        if not fat_offsets:
            print("No FAT signatures found.")
            continue

        # Step 3: Determine FAT parameters
        fat_start = fat_offsets[0]
        reserved_sectors = fat_start // sector_size
        num_fats = len(fat_offsets)
        sectors_per_fat = (fat_offsets[1] - fat_offsets[0]) // sector_size if num_fats >= 2 else formats[0].get('sectors_per_fat', 2)

        # Step 4: Use root_entries from matching formats
        re = formats[0]['root_directory']  # All matching formats have the same root_directory
        root_sectors = math.ceil(re * 32 / sector_size)  # Each entry is 32 bytes
        data_start_sector = reserved_sectors + num_fats * sectors_per_fat + root_sectors

        if data_start_sector >= total_sectors:
            print("Data start sector exceeds total sectors.")
            continue

        data_sectors = total_sectors - data_start_sector
        possible_sc = []
        for sc in [1, 2, 4, 8, 16]:  # Reasonable range for floppy disks
            if data_sectors % sc != 0:
                continue
            num_clusters = data_sectors // sc
            if num_clusters < 2:  # Minimum clusters for FAT12
                continue
            fat_bytes_needed = math.ceil((num_clusters + 2) * 1.5)  # 12 bits per cluster
            fat_sectors_needed = math.ceil(fat_bytes_needed / sector_size)
            if fat_sectors_needed <= sectors_per_fat:
                possible_sc.append(sc)

        if not possible_sc:
            print("No suitable sectors_per_cluster found.")
            continue

        # Step 5: Choose sc=1 if possible, else smallest sc
        sc = 1 if 1 in possible_sc else min(possible_sc)

        # Step 6: Set parameters
        params = {
            'bytes_per_sector': sector_size,
            'sectors_per_cluster': sc,
            'reserved_sectors': reserved_sectors,
            'num_fats': num_fats,
            'root_entries': re,
            'total_sectors': total_sectors,
            'media_descriptor': image_data[fat_start],
            'sectors_per_fat': sectors_per_fat,
            'fat_offsets': fat_offsets[:num_fats]
        }

        print(f"Detected FAT12 params: {params}")
        return params

    print("Could not detect FAT12 parameters.")
    return None

def main():
    image = input("Enter the path to the floppy image: ")
    with open(image, 'rb') as f:
        image_data = f.read()
    params = detect_fat12_params(image_data)
    if params:
        print(f"Final detected FAT12 params: {params}")
    else:
        print("Failed to detect FAT12 parameters.")

if __name__ == "__main__":
    main()
