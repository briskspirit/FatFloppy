# src/fatfloppy/core/disk.py
from dataclasses import dataclass
from typing import Optional, Tuple

from .utils.logging_config import get_logger
from .drivers import DiskIODriver, PhysicalFormat

logger = get_logger()

@dataclass
class DiskGeometry:
    cylinders: int
    heads: int
    sectors_per_track: int
    sector_size: int

    @property
    def total_sectors(self) -> int:
        return self.cylinders * self.heads * self.sectors_per_track

    @property
    def total_bytes(self) -> int:
        return self.total_sectors * self.sector_size

class Disk:
    def __init__(self, driver: DiskIODriver):
        self.logger = get_logger(self.__class__.__name__)
        self.driver = driver
        self.geometry: Optional[DiskGeometry] = None
        self.logger.debug("Disk object initialized")

    def set_geometry(self, geometry: DiskGeometry) -> None:
        """Sets the disk geometry and attempts to update the driver's physical format."""
        if not isinstance(geometry, DiskGeometry):
            raise TypeError("geometry must be a DiskGeometry object")

        self.logger.info(f"Setting disk geometry: C:{geometry.cylinders}, H:{geometry.heads}, "
                        f"SPT:{geometry.sectors_per_track}, SectorSize:{geometry.sector_size}")
        self.geometry = geometry

        # Attempt to update the driver's physical format if the driver supports it
        if hasattr(self.driver, "set_physical_format"):
            try:
                # Try to get the existing format
                existing_pf = getattr(self.driver, "physical_format", None)

                if not existing_pf:
                    # If driver supports it but has no format, create a basic one
                    self.logger.debug("Driver supports physical format, creating default based on geometry")
                    # Use sensible defaults, geometry provides the core parameters
                    physical_format = PhysicalFormat(
                         encoding="MFM", rate=500, rpm=300, gap3=84, # Common defaults
                         sectors_per_track=geometry.sectors_per_track,
                         heads=geometry.heads,
                         sector_size=geometry.sector_size
                    )
                    self.driver.set_physical_format(physical_format)
                    self.logger.info("Set new default physical format in driver.")
                else:
                    # Update existing physical format only with geometry parts
                    self.logger.debug("Updating existing driver physical format with new geometry parameters.")
                    existing_pf.sectors_per_track = geometry.sectors_per_track
                    existing_pf.heads = geometry.heads
                    existing_pf.sector_size = geometry.sector_size
                    # Inform the driver of the change (if it needs internal updates)
                    self.driver.set_physical_format(existing_pf)
                    self.logger.info("Updated driver's physical format with new geometry.")

            except Exception as e:
                 self.logger.error(f"Failed to set or update physical format in driver: {e}", exc_info=True)
        else:
            self.logger.debug("Driver does not have set_physical_format method, skipping update.")

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        """Reads a single sector using CHS addressing."""
        if not self.geometry:
            error_msg = "Disk geometry not set, cannot read sector"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        # Validate CHS address against geometry
        if not (0 <= cylinder < self.geometry.cylinders and
                0 <= head < self.geometry.heads and
                1 <= sector <= self.geometry.sectors_per_track):
            error_msg = f"Invalid sector address: C:{cylinder} H:{head} S:{sector} (Geom: C:{self.geometry.cylinders} H:{self.geometry.heads} SPT:{self.geometry.sectors_per_track})"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        # self.logger.debug(f"Reading sector C:{cylinder} H:{head} S:{sector}")
        try:
            data = self.driver.read_sector(cylinder, head, sector)
            # self.logger.debug(f"Read {len(data)} bytes from sector C:{cylinder} H:{head} S:{sector}")

            # Ensure returned data matches expected sector size (pad if necessary)
            if len(data) < self.geometry.sector_size:
                self.logger.warning(f"Driver returned short read ({len(data)} bytes) for C:{cylinder} H:{head} S:{sector}. Padding to {self.geometry.sector_size} bytes.")
                data += bytes(self.geometry.sector_size - len(data))
            elif len(data) > self.geometry.sector_size:
                 self.logger.warning(f"Driver returned long read ({len(data)} bytes) for C:{cylinder} H:{head} S:{sector}. Truncating to {self.geometry.sector_size} bytes.")
                 data = data[:self.geometry.sector_size]

            return data
        except Exception as e:
             self.logger.error(f"Driver error reading sector C:{cylinder} H:{head} S:{sector}: {e}", exc_info=True)
             # Re-raise as a more specific error maybe? Or return empty sector?
             # Raising seems better to indicate failure.
             raise IOError(f"Failed to read sector C:{cylinder} H:{head} S:{sector}") from e


    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        """Writes data to a single sector using CHS addressing."""
        if not self.geometry:
            error_msg = "Disk geometry not set, cannot write sector"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        # Validate CHS address against geometry
        if not (0 <= cylinder < self.geometry.cylinders and
                0 <= head < self.geometry.heads and
                1 <= sector <= self.geometry.sectors_per_track):
            error_msg = f"Invalid sector address: C:{cylinder} H:{head} S:{sector} (Geom: C:{self.geometry.cylinders} H:{self.geometry.heads} SPT:{self.geometry.sectors_per_track})"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        # Validate data size
        if len(data) != self.geometry.sector_size:
            error_msg = f"Data size {len(data)} does not match geometry sector size {self.geometry.sector_size} for C:{cylinder} H:{head} S:{sector}"
            # Option 1: Raise Error (Strict)
            self.logger.error(error_msg)
            raise ValueError(error_msg)
            # Option 2: Pad or Truncate (Lenient, might hide issues)
            # self.logger.warning(error_msg + ". Adjusting data size.")
            # if len(data) < self.geometry.sector_size:
                # data = data + bytes(self.geometry.sector_size - len(data))
            # else:
                # data = data[:self.geometry.sector_size]

        # self.logger.debug(f"Writing {len(data)} bytes to sector C:{cylinder} H:{head} S:{sector}")
        try:
            self.driver.write_sector(cylinder, head, sector, data)
            # self.logger.debug(f"Write successful for C:{cylinder} H:{head} S:{sector}")
        except Exception as e:
            self.logger.error(f"Driver error writing sector C:{cylinder} H:{head} S:{sector}: {e}", exc_info=True)
            raise IOError(f"Failed to write sector C:{cylinder} H:{head} S:{sector}") from e

    def read_sectors(self, start_cylinder: int, start_head: int, start_sector: int,
                     num_sectors: int) -> bytes:
        """Reads a sequence of contiguous sectors."""
        if not self.geometry:
            error_msg = "Disk geometry not set, cannot read sectors"
            self.logger.error(error_msg)
            raise ValueError(error_msg)
        if num_sectors <= 0:
            return b''

        self.logger.debug(f"Reading {num_sectors} sectors starting at C:{start_cylinder} H:{start_head} S:{start_sector}")
        result = bytearray()

        # Check if driver supports multi-sector reads directly (potential optimization)
        # if hasattr(self.driver, 'read_sectors_direct'):
        #     try:
        #         # This would require the driver to handle CHS iteration
        #         data = self.driver.read_sectors_direct(start_cylinder, start_head, start_sector, num_sectors)
        #         if len(data) == num_sectors * self.geometry.sector_size:
        #             self.logger.debug(f"Driver read {len(data)} bytes directly.")
        #             return data
        #         else:
        #             self.logger.warning("Driver read_sectors_direct returned unexpected size, falling back.")
        #     except NotImplementedError:
        #         pass # Fallback to manual iteration
        #     except Exception as e:
        #         self.logger.warning(f"Driver read_sectors_direct failed: {e}. Falling back.")

        # Manual iteration using read_sector
        cylinder, head, sector = start_cylinder, start_head, start_sector
        sectors_per_track = self.geometry.sectors_per_track
        heads = self.geometry.heads

        for i in range(num_sectors):
            # Check bounds before reading each sector
            if not (0 <= cylinder < self.geometry.cylinders and 0 <= head < heads and 1 <= sector <= sectors_per_track):
                 error_msg = f"Calculated invalid address during multi-sector read: C:{cylinder} H:{head} S:{sector} (request started at C:{start_cylinder} H:{start_head} S:{start_sector} for {num_sectors})"
                 self.logger.error(error_msg)
                 raise ValueError(error_msg)

            # self.logger.debug(f"Reading sector {i+1}/{num_sectors}: C:{cylinder} H:{head} S:{sector}")
            result.extend(self.read_sector(cylinder, head, sector)) # read_sector handles padding

            # Increment CHS address
            sector += 1
            if sector > sectors_per_track:
                sector = 1
                head += 1
                if head >= heads:
                    head = 0
                    cylinder += 1

        expected_size = num_sectors * self.geometry.sector_size
        if len(result) != expected_size:
             # This shouldn't happen if read_sector pads correctly, but log if it does
             self.logger.warning(f"Multi-sector read result size {len(result)} differs from expected {expected_size}")

        self.logger.debug(f"Completed multi-sector read, got {len(result)} bytes")
        return bytes(result)

    def write_sectors(self, start_cylinder: int, start_head: int, start_sector: int,
                      data: bytes) -> None:
        """Writes data across a sequence of contiguous sectors."""
        if not self.geometry:
            error_msg = "Disk geometry not set, cannot write sectors"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        if not data:
            self.logger.warning("Write sectors called with no data")
            return

        sector_size = self.geometry.sector_size
        num_sectors = (len(data) + sector_size - 1) // sector_size
        self.logger.debug(f"Writing {len(data)} bytes to {num_sectors} sectors starting at C:{start_cylinder} H:{start_head} S:{start_sector}")

        # Check if driver supports multi-sector writes directly
        # if hasattr(self.driver, 'write_sectors_direct'):
        #      try:
        #         # Pad data to full sector multiple if needed by driver
        #         padded_data = data
        #         if len(data) % sector_size != 0:
        #              padding = sector_size - (len(data) % sector_size)
        #              padded_data += bytes(padding)
        #         self.driver.write_sectors_direct(start_cylinder, start_head, start_sector, padded_data)
        #         self.logger.debug(f"Driver wrote {len(data)} bytes ({num_sectors} sectors) directly.")
        #         return # Done if direct write succeeded
        #      except NotImplementedError:
        #         pass # Fallback
        #      except Exception as e:
        #         self.logger.warning(f"Driver write_sectors_direct failed: {e}. Falling back.")

        # Manual iteration using write_sector
        cylinder, head, sector = start_cylinder, start_head, start_sector
        sectors_per_track = self.geometry.sectors_per_track
        heads = self.geometry.heads
        data_pos = 0

        for i in range(num_sectors):
            # Check bounds before writing each sector
            if not (0 <= cylinder < self.geometry.cylinders and 0 <= head < heads and 1 <= sector <= sectors_per_track):
                 error_msg = f"Calculated invalid address during multi-sector write: C:{cylinder} H:{head} S:{sector} (request started at C:{start_cylinder} H:{start_head} S:{start_sector} for {num_sectors})"
                 self.logger.error(error_msg)
                 raise ValueError(error_msg)

            chunk_start = i * sector_size
            # Use data_pos to track position in input data
            chunk_end = min(data_pos + sector_size, len(data))
            chunk = data[data_pos:chunk_end]

            # Pad the chunk if it's the last part and doesn't fill the sector
            if len(chunk) < sector_size:
                # self.logger.debug(f"Padding last chunk from {len(chunk)} to {sector_size} bytes for C:{cylinder} H:{head} S:{sector}")
                chunk = chunk + bytes(sector_size - len(chunk))

            # self.logger.debug(f"Writing sector {i+1}/{num_sectors}: C:{cylinder} H:{head} S:{sector}")
            self.write_sector(cylinder, head, sector, chunk) # write_sector handles size adjustment if needed
            data_pos += len(chunk) # Increment by sector_size (or adjusted size)

            # Increment CHS address
            sector += 1
            if sector > sectors_per_track:
                sector = 1
                head += 1
                if head >= heads:
                    head = 0
                    cylinder += 1

        self.logger.debug(f"Completed multi-sector write of {len(data)} bytes to {num_sectors} sectors")

    def flush(self) -> None:
        """Flushes any buffered changes in the driver to the physical media/image."""
        self.logger.debug("Flushing disk changes via driver")
        if hasattr(self.driver, 'flush'):
            try:
                self.driver.flush()
                self.logger.debug("Driver flush completed.")
            except Exception as e:
                 self.logger.error(f"Driver flush failed: {e}", exc_info=True)
                 # Re-raise? depends on expected behavior
                 raise IOError("Disk flush operation failed") from e
        else:
             self.logger.debug("Driver does not support flush operation.")

    # --- Geometry / LBA / CHS --- MOVED HERE ---
    def lba_to_chs(self, lba: int) -> Tuple[int, int, int]:
        """Converts Logical Block Address (LBA) to Cylinder, Head, Sector (CHS)."""
        # Use self.geometry directly
        if not self.geometry:
            raise ValueError("Cannot convert LBA to CHS: Disk geometry not set.")

        geom = self.geometry # Use self.geometry
        if geom.sectors_per_track == 0 or geom.heads == 0:
             raise ValueError(f"Invalid geometry prevents LBA->CHS conversion (SPT={geom.sectors_per_track}, Heads={geom.heads})")

        # Check LBA bounds
        max_lba = geom.total_sectors - 1
        if not (0 <= lba <= max_lba):
            # Use self.logger for consistency within the Disk class
            self.logger.warning(f"LBA {lba} is out of bounds (0-{max_lba}) for current geometry.")
            # Optionally raise error, or clamp/return indicative values
            # Raising seems safer to prevent unexpected behaviour
            raise IndexError(f"LBA {lba} out of bounds for geometry (0-{max_lba})")

        sector = (lba % geom.sectors_per_track) + 1 # Sector is 1-based
        temp = lba // geom.sectors_per_track
        head = temp % geom.heads
        cylinder = temp // geom.heads

        # self.logger.debug(f"LBA {lba} -> C:{cylinder} H:{head} S:{sector}")
        return cylinder, head, sector

    # Note: _chs_to_lba might be useful but is not strictly needed for current ops
    # def chs_to_lba(self, cylinder: int, head: int, sector: int) -> int:
    #     """Converts Cylinder, Head, Sector (CHS) to Logical Block Address (LBA)."""
    #     if not self.geometry:
    #         raise ValueError("Cannot convert CHS to LBA: Disk geometry not set.")
    #     # ... implementation ...
