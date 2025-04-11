# TODO: Add media descriptor to the formats
# MDB with 0x00 means unknown media descriptor for that disk type
FLOPPY_FORMATS = [
    {"size": "8\"",    "type": "SD", "mdb":0xFE, "heads": 1, "tracks": 77, "sectors": 26,   "sector_size": 128,     "total_sectors": 2002,  "root_directory": 68,   "capacity": 256256,     "rpm": 360, "encoding": "FM",  "codec": None}, # 68 root dirs for SCP, 64 for 86-DOS
    {"size": "8\"",    "type": "SD", "mdb":0xFE, "heads": 1, "tracks": 77, "sectors": 15,   "sector_size": 256,     "total_sectors": 1155,  "root_directory": 56,   "capacity": 295680,     "rpm": 360, "encoding": "FM",  "codec": None},
    {"size": "8\"",    "type": "SD", "mdb":0xFE, "heads": 1, "tracks": 77, "sectors": 8,    "sector_size": 512,     "total_sectors": 616,   "root_directory": 64,   "capacity": 315392,     "rpm": 360, "encoding": "FM",  "codec": None},
    {"size": "8\"",    "type": "SD", "mdb":0xFD, "heads": 2, "tracks": 77, "sectors": 26,   "sector_size": 128,     "total_sectors": 4004,  "root_directory": 96,   "capacity": 512512,     "rpm": 360, "encoding": "FM",  "codec": None},
    {"size": "8\"",    "type": "SD", "mdb":0xFD, "heads": 2, "tracks": 77, "sectors": 15,   "sector_size": 256,     "total_sectors": 2310,  "root_directory": 96,   "capacity": 591360,     "rpm": 360, "encoding": "FM",  "codec": None},

    {"size": "8\"",    "type": "DD", "mdb":0x00, "heads": 1, "tracks": 77, "sectors": 8,    "sector_size": 1024,    "total_sectors": 616,   "root_directory": 96,   "capacity": 630784,     "rpm": 360, "encoding": "MFM", "codec": None},
    {"size": "8\"",    "type": "DD", "mdb":0xFE, "heads": 2, "tracks": 77, "sectors": 26,   "sector_size": 256,     "total_sectors": 4004,  "root_directory": 192,  "capacity": 1025024,    "rpm": 360, "encoding": "MFM", "codec": None},
    {"size": "8\"",    "type": "DD", "mdb":0xFE, "heads": 2, "tracks": 77, "sectors": 15,   "sector_size": 512,     "total_sectors": 2310,  "root_directory": 192,  "capacity": 1182720,    "rpm": 360, "encoding": "MFM", "codec": None},
    {"size": "8\"",    "type": "DD", "mdb":0xFE, "heads": 2, "tracks": 77, "sectors": 8,    "sector_size": 1024,    "total_sectors": 1232,  "root_directory": 192,  "capacity": 1261568,    "rpm": 360, "encoding": "MFM", "codec": None}, # 86-DOS has 128 root dirs

    {"size": "5.25\"", "type": "DD", "mdb":0xFE, "heads": 1, "tracks": 40, "sectors": 8,    "sector_size": 512,     "total_sectors": 320,   "root_directory": 64,   "capacity": 163840,     "rpm": 300, "encoding": "MFM", "codec": "ibm.160"},
    {"size": "5.25\"", "type": "DD", "mdb":0xFF, "heads": 2, "tracks": 40, "sectors": 8,    "sector_size": 512,     "total_sectors": 640,   "root_directory": 112,  "capacity": 327680,     "rpm": 300, "encoding": "MFM", "codec": "ibm.320"},
    {"size": "5.25\"", "type": "DD", "mdb":0xFC, "heads": 1, "tracks": 40, "sectors": 9,    "sector_size": 512,     "total_sectors": 360,   "root_directory": 64,   "capacity": 184320,     "rpm": 300, "encoding": "MFM", "codec": "ibm.180"},
    {"size": "5.25\"", "type": "DD", "mdb":0xFD, "heads": 2, "tracks": 40, "sectors": 9,    "sector_size": 512,     "total_sectors": 720,   "root_directory": 112,  "capacity": 368640,     "rpm": 300, "encoding": "MFM", "codec": "ibm.360"},

    {"size": "5.25\"", "type": "HD", "mdb":0xF9, "heads": 2, "tracks": 80, "sectors": 15,   "sector_size": 512,     "total_sectors": 2400,  "root_directory": 224,  "capacity": 1228800,    "rpm": 360, "encoding": "MFM", "codec": "ibm.1200"},

    {"size": "3.5\"",  "type": "DD", "mdb":0xFE, "heads": 1, "tracks": 80, "sectors": 8,    "sector_size": 512,     "total_sectors": 640,   "root_directory": 112,  "capacity": 327680,     "rpm": 300, "encoding": "MFM", "codec": "ibm.160"},
    {"size": "3.5\"",  "type": "DD", "mdb":0xFC, "heads": 1, "tracks": 80, "sectors": 9,    "sector_size": 512,     "total_sectors": 720,   "root_directory": 112,  "capacity": 368640,     "rpm": 300, "encoding": "MFM", "codec": "ibm.180"},
    {"size": "3.5\"",  "type": "DD", "mdb":0x00, "heads": 2, "tracks": 80, "sectors": 8,    "sector_size": 512,     "total_sectors": 1280,  "root_directory": 112,  "capacity": 655360,     "rpm": 300, "encoding": "MFM", "codec": None},
    {"size": "3.5\"",  "type": "DD", "mdb":0xF9, "heads": 2, "tracks": 80, "sectors": 9,    "sector_size": 512,     "total_sectors": 1440,  "root_directory": 112,  "capacity": 737280,     "rpm": 300, "encoding": "MFM", "codec": "ibm.720"},

    {"size": "3.5\"",  "type": "HD", "mdb":0xF0, "heads": 2, "tracks": 80, "sectors": 18,   "sector_size": 512,     "total_sectors": 2880,  "root_directory": 224,  "capacity": 1474560,    "rpm": 300, "encoding": "MFM", "codec": "ibm.1440"},
    # Special DMF formats
    {"size": "3.5\"",  "type": "HD", "mdb":0xF0, "heads": 2, "tracks": 80, "sectors": 21,   "sector_size": 512,     "total_sectors": 3360,  "root_directory": 16,   "capacity": 1720320,    "rpm": 300, "encoding": "MFM", "codec": "ibm.1680"},
    {"size": "3.5\"",  "type": "HD", "mdb":0xF0, "heads": 2, "tracks": 82, "sectors": 21,   "sector_size": 512,     "total_sectors": 3444,  "root_directory": 16,   "capacity": 1763328,    "rpm": 300, "encoding": "MFM", "codec": None},

    {"size": "3.5\"",  "type": "ED", "mdb":0xF0, "heads": 2, "tracks": 80, "sectors": 36,   "sector_size": 512,     "total_sectors": 5760,  "root_directory": 224,  "capacity": 2949120,    "rpm": 300, "encoding": "MFM", "codec": "ibm.2880"},
]
