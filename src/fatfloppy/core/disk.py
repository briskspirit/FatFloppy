from typing import Optional

from .drivers.base_driver import DiskIODriver
from .physical_format import PhysicalFormat
from .utils.logging_config import get_logger

logger = get_logger()


class Disk:
    """
    Represents a disk, providing an abstraction over a DiskIODriver.

    This class uses a PhysicalFormat object to interpret the disk's geometry
    and provides methods for reading and writing sectors using CHS (Cylinder,
    Head, Sector) or LBA (Logical Block Addressing) addressing.
    """

    def __init__(self, driver: DiskIODriver):
        """
        Initializes the Disk object.

        Args:
            driver: An instance of a DiskIODriver subclass that will be used
                for all underlying I/O operations.
        """
        self.logger = get_logger(self.__class__.__name__)
        self.driver: DiskIODriver = driver
        self.physical_format: Optional[PhysicalFormat] = None

    def flush(self) -> None:
        """
        Flushes any buffered writes in the underlying driver to the disk medium.

        Raises:
            IOError: If the flush operation fails at the driver level.
        """
        if hasattr(self.driver, "flush"):
            self.logger.debug("Flushing disk")
            try:
                self.driver.flush()
            except Exception as e:
                self.logger.error(f"Disk flush failed: {e}", exc_info=True)
                raise IOError("Disk flush failed") from e

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        """
        Reads a single sector from the disk at the given CHS address.

        Args:
            cylinder: The cylinder number.
            head: The head number.
            sector: The sector number.

        Returns:
            A bytes object containing the sector data. The data is padded with
            zeros or truncated to match the expected sector size.

        Raises:
            ValueError: If the disk geometry has not been set.
            IOError: If the read operation fails at the driver level.
        """
        self._validate_chs(cylinder, head, sector)
        self.logger.debug(f"Reading sector C:{cylinder} H:{head} S:{sector}")
        try:
            bytes_per_sector = self.physical_format.get_bytes_per_sector(
                cylinder,
                head
            )
            physical_head = (
                self.physical_format.get_physical_head(head)
                if getattr(self.driver, 'uses_physical_heads', False)
                else head
            )
            data = self.driver.read_sector(cylinder, physical_head, sector)

            if len(data) < bytes_per_sector:
                data += bytes(bytes_per_sector - len(data))
            elif len(data) > bytes_per_sector:
                data = data[:bytes_per_sector]
            return data
        except Exception as e:
            self.logger.error(
                f"Failed to read sector C:{cylinder} H:{head} S:{sector}: {e}",
                exc_info=True
            )
            raise IOError(
                f"Failed to read sector C:{cylinder} H:{head} S:{sector}"
            ) from e

    def read_sectors(
        self,
        start_cylinder: int,
        start_head: int,
        start_sector: int,
        num_sectors: int
    ) -> bytes:
        """
        Reads a contiguous sequence of sectors from the disk.

        Args:
            start_cylinder: The starting cylinder number.
            start_head: The starting head number.
            start_sector: The starting sector number.
            num_sectors: The total number of sectors to read.

        Returns:
            A bytes object containing the combined data from all read sectors.

        Raises:
            ValueError: If the disk geometry has not been set.
        """
        if not self.physical_format:
            raise ValueError("Disk geometry not set")
        if num_sectors <= 0:
            return b''

        self.logger.debug(
            f"Reading {num_sectors} sectors starting at C:{start_cylinder} "
            f"H:{start_head} S:{start_sector}"
        )
        result = bytearray()
        cylinder, head, sector = start_cylinder, start_head, start_sector

        for _ in range(num_sectors):
            self.physical_format.validate_chs(cylinder, head, sector)
            result.extend(self.read_sector(cylinder, head, sector))

            sector += 1
            if sector > self.physical_format.get_sectors_per_track(
                cylinder,
                head
            ):
                sector = 1
                head += 1
                if head >= self.physical_format.heads:
                    head = 0
                    cylinder += 1
        return bytes(result)

    def set_geometry(self, geometry: PhysicalFormat) -> None:
        """
        Sets the physical geometry for the disk and notifies the driver.

        Args:
            geometry: A PhysicalFormat object describing the disk's layout.

        Raises:
            TypeError: If the provided geometry is not a PhysicalFormat object.
        """
        if not isinstance(geometry, PhysicalFormat):
            raise TypeError("geometry must be a PhysicalFormat object")

        self.physical_format = geometry
        self.logger.info(
            f"Disk geometry set: default bytes_per_sector="
            f"{geometry.bytes_per_sector}"
        )

        if hasattr(self.driver, "set_physical_format"):
            try:
                self.driver.set_physical_format(geometry)
                self.logger.debug("Physical format applied to driver")
            except Exception as e:
                self.logger.error(
                    f"Failed to set physical format on driver: {e}",
                    exc_info=True
                )
                raise

    def write_sector(
        self,
        cylinder: int,
        head: int,
        sector: int,
        data: bytes
    ) -> None:
        """
        Writes a single sector to the disk at the given CHS address.

        Args:
            cylinder: The cylinder number.
            head: The head number.
            sector: The sector number.
            data: The byte data to write. Must match the sector size.

        Raises:
            ValueError: If the data size does not match the sector size or if
                disk geometry is not set.
            IOError: If the write operation fails at the driver level.
        """
        self._validate_chs(cylinder, head, sector)
        bytes_per_sector = self.physical_format.get_bytes_per_sector(
            cylinder,
            head
        )
        if len(data) != bytes_per_sector:
            raise ValueError(
                f"Data size {len(data)} != sector size {bytes_per_sector}"
            )

        self.logger.debug(f"Writing sector C:{cylinder} H:{head} S:{sector}")
        try:
            physical_head = (
                self.physical_format.get_physical_head(head)
                if getattr(self.driver, 'uses_physical_heads', False)
                else head
            )
            self.driver.write_sector(cylinder, physical_head, sector, data)
        except Exception as e:
            self.logger.error(
                f"Failed to write sector C:{cylinder} H:{head} S:{sector}: {e}",
                exc_info=True
            )
            raise IOError(
                f"Failed to write sector C:{cylinder} H:{head} S:{sector}"
            ) from e

    def write_sectors(
        self,
        start_cylinder: int,
        start_head: int,
        start_sector: int,
        data: bytes
    ) -> None:
        """
        Writes a contiguous sequence of sectors to the disk.

        Args:
            start_cylinder: The starting cylinder number.
            start_head: The starting head number.
            start_sector: The starting sector number.
            data: The byte data to write. If the data length is not a multiple
                of the sector size, it will be padded with zeros.

        Raises:
            ValueError: If the disk geometry has not been set.
        """
        if not self.physical_format:
            raise ValueError("Disk geometry not set")
        if not data:
            return

        self.logger.debug(
            f"Writing data of size {len(data)} starting at C:{start_cylinder} "
            f"H:{start_head} S:{start_sector}"
        )
        cylinder, head, sector = start_cylinder, start_head, start_sector
        data_pos = 0

        while data_pos < len(data):
            self.physical_format.validate_chs(cylinder, head, sector)
            bytes_per_sector = self.physical_format.get_bytes_per_sector(
                cylinder,
                head
            )

            chunk = data[data_pos:data_pos + bytes_per_sector]
            if len(chunk) < bytes_per_sector:
                chunk += bytes(bytes_per_sector - len(chunk))

            self.write_sector(cylinder, head, sector, chunk)
            data_pos += bytes_per_sector

            sector += 1
            if sector > self.physical_format.get_sectors_per_track(
                cylinder,
                head
            ):
                sector = 1
                head += 1
                if head >= self.physical_format.heads:
                    head = 0
                    cylinder += 1

    def _validate_chs(self, cylinder: int, head: int, sector: int) -> None:
        """
        Validates CHS coordinates against the current physical format.

        Args:
            cylinder: The cylinder number to validate.
            head: The head number to validate.
            sector: The sector number to validate.

        Raises:
            ValueError: If disk geometry is not set.
            IndexError: If any CHS value is out of bounds.
        """
        if not self.physical_format:
            raise ValueError("Disk geometry not set")
        self.physical_format.validate_chs(cylinder, head, sector)
