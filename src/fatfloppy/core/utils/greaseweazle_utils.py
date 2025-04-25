# src/fatfloppy/core/utils/greaseweazle_utils.py
import logging
from typing import Optional

from greaseweazle.codec import codec
from greaseweazle.codec.ibm import ibm
from ..physical_format import PhysicalFormat, TrackFormat

def create_greaseweazle_diskdef(
    physical_format: PhysicalFormat,
    logger: logging.Logger,
) -> Optional[codec.DiskDef]:
    if not physical_format:
        logger.warning("Cannot create custom diskdef: physical format not provided")
        return None

    logger.debug("Creating custom disk definition")

    try:
        disk_def = codec.DiskDef()
        disk_def.cyls = physical_format.cylinders
        disk_def.heads = physical_format.heads

        # Create track definitions for each TrackFormat
        for tf in physical_format.track_formats:
            # Determine format name based on encoding
            if tf.encoding == "MFM":
                format_name = "ibm.mfm"
            elif tf.encoding == "FM":
                format_name = "ibm.fm"
            else:
                logger.warning(f"Unsupported encoding '{tf.encoding}' for track format, defaulting to ibm.mfm")
                format_name = "ibm.mfm"

            # Create a track definition for this TrackFormat
            track_def = ibm.IBMTrack_FixedDef(format_name)
            track_def.add_param("secs", str(tf.sectors_per_track))
            track_def.add_param("bps", str(physical_format.bytes_per_sector))
            track_def.add_param("gap3", str(tf.gap3))
            track_def.add_param("rate", str(tf.rate))
            track_def.finalise()

            # Assign this track definition to the specified cylinder and head ranges
            for c in range(tf.track_start, tf.track_end + 1):
                for h in range(tf.head_start, tf.head_end + 1):
                    disk_def.track_map[(c, h)] = track_def

        disk_def.finalise()

        logger.info(
            f"Custom disk definition created: Cyls={physical_format.cylinders}, Heads={physical_format.heads}, "
            f"with {len(physical_format.track_formats)} track formats"
        )
        return disk_def
    except Exception as e:
        logger.error(f"Failed to create custom disk definition: {e}", exc_info=True)
        return None
