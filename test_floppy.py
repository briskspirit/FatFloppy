from diskmanager import FloppyDiskManager
from floppybpb import FloppyBPB


format_params = {
    'cyls': 80,            # Number of cylinders
    'heads': 2,            # Number of heads
    'format_name': 'ibm.mfm',  # Use 'ibm.mfm' for MFM encoding, 'ibm.fm' for FM
    'track_params': {
        'secs': 18,        # Sectors per track
        'bps': 512,        # Bytes per sector
        'gap3': 84,        # Gap 3 size (post-DAM)
        'rate': 500,       # Data rate in kbps (e.g., 500 for HD)
        # Optional additional parameters:
        # 'interleave': 2, # Sector interleave factor
        # 'gap1': 50,      # Post-IAM gap
        # 'gap2': 22,      # Post-IDAM gap
        # 'rpm': 300,      # Rotations per minute
        # 'iam': 'yes',    # Include Index Address Mark (yes/no)
    }
}



if __name__ == "__main__":
    from floppybpb import FloppyBPB
    try:
        # Example: Test FloppyDiskManager
        disk_manager = FloppyDiskManager(format_name='ibm.1440')
        # disk_manager = FloppyDiskManager(format_params=format_params)
        # Read boot sector (geometry will be set by FloppyBPB)
        bpb = FloppyBPB(disk_manager)
        print(f"Boot sector OEM ID: {bpb.oem_id}")
        print(f"Boot sector OEM ID: {bpb.total_sectors}")
        # Example write (uncomment with caution)
        disk_manager.write_bytes(3, 'MSDOS7.0'.encode('cp437').ljust(8))
        disk_manager.flush()
        print("FloppyDiskManager test completed.")
    except Exception as e:
        print(f"Error during FloppyDiskManager test: {e}")
