ALTER TABLE edge_devices ADD COLUMN mac_address TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_edge_devices_mac_address
    ON edge_devices(mac_address) WHERE mac_address IS NOT NULL;
