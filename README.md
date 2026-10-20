# CATui Bot

Private Discord control for CATui emulator updates.

Checks upstream commits against the source bases in the CATui store, shows the latest release when available, and asks for approval before updating a core. Supports PCSX2, Eden, RPCS3, shadPS4, PPSSPP, Cemu and Flycast; comparisons require a saved source baseline.

- `/cores-panel` - choose which cores to monitor.
- `/cores-check` - check commits and review available updates.
- `/chat` - talk to the CATui assistant.
- `/issue` - convert a Discord issue gallery post into a simplified GitHub issue on `arthiee4/catui-main`.
- `/release` - format the latest CATui release into the official Discord release announcement template.
