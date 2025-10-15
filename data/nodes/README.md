# Per-node data directories

This directory will contain one subfolder per attached Meshtastic node, named by a stable `node_uid`.

Example layout:

- nodes/
  - asd/
    - nodes.csv
    - sightings.csv
    - threads/
      - channels/
        - general.csv
      - dms/
        - 0x123abc.csv
    - state/

Notes:
- Each node_uid directory is independent; scripts should not cross-write between directories.
- Locks are per-file per node; multiple nodes can be processed concurrently.
