# src/fatfloppy/core/utils/greaseweazle_utils.py
import logging
from typing import Optional

from greaseweazle.codec import codec
from greaseweazle.codec.ibm import ibm
from ..physical_format import PhysicalFormat

def create_greaseweazle_diskdef(
    physical_format: PhysicalFormat,
    logger: logging.Logger,
    cylinders: Optional[int] = None,
    drive_size: Optional[str] = None,
) -> Optional[codec.DiskDef]:
    if not physical_format:
        logger.warning("Cannot create custom diskdef: physical format not provided")
        return None

    logger.debug("Creating custom disk definition")

    if cylinders is not None:
        final_cylinders = cylinders
        logger.debug(f"Using explicitly provided cylinder count: {final_cylinders}")
    else:
        logger.debug("Determining cylinder count internally based on drive size/rate...")
        final_cylinders = 80
        if drive_size == "5.25":
            if physical_format.rate == 250:
                final_cylinders = 40
        logger.debug(f"Internally determined cylinder count: {final_cylinders}")

    params = {
        'cyls': final_cylinders,
        'heads': physical_format.heads,
        'sectors_per_track': physical_format.sectors_per_track,
        'sector_size': physical_format.sector_size,
        'encoding': physical_format.encoding,
        'rate': physical_format.rate,
        'gap3': physical_format.gap3
    }

    try:
        disk_def = codec.DiskDef()
        disk_def.cyls = params['cyls']
        disk_def.heads = params['heads']

        if params['encoding'] == "MFM":
            format_name = "ibm.mfm"
        elif params['encoding'] == "FM":
            format_name = "ibm.fm"
        else:
            logger.warning(f"Unsupported encoding '{params['encoding']}' for custom diskdef, defaulting to ibm.mfm")
            format_name = "ibm.mfm"

        track_def = ibm.IBMTrack_FixedDef(format_name)
        track_def.add_param("secs", str(params['sectors_per_track']))
        track_def.add_param("bps", str(params['sector_size']))
        track_def.add_param("gap3", str(params['gap3']))
        track_def.add_param("rate", str(params['rate']))
        track_def.finalise()

        for c in range(disk_def.cyls):
            for h in range(disk_def.heads):
                disk_def.track_map[(c, h)] = track_def

        disk_def.finalise()

        logger.info(
            f"Custom disk definition created: Cyls={params['cyls']}, Heads={params['heads']}, "
            f"{params['sectors_per_track']} sectors, {params['sector_size']} bytes/sector, {params['encoding']} encoding"
        )
        return disk_def
    except Exception as e:
        logger.error(f"Failed to create custom disk definition: {e}", exc_info=True)
        return None
