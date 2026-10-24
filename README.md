# CATui Bot

Private Discord control for CATui emulator updates.

Checks upstream commits against the source bases in the CATui store, shows the latest release when available, and asks for approval before updating a core. Supports PCSX2, Eden, RPCS3, shadPS4, PPSSPP, Cemu and Flycast; comparisons require a saved source baseline.

The bot also checks published CATui releases every five minutes and posts new release notes to the configured announcement channel. It formats the official GitHub release body, keeps links outside the YAML block, and stores a cursor so restarts do not repost releases. On the first startup with an empty state database, the current latest release is recorded as the baseline.

- `/cores-panel` - choose which cores to monitor.
- `/cores-check` - check commits and review available updates.
- `/chat` - talk to the CATui assistant.
- `/issue` - convert a Discord issue gallery post into a simplified GitHub issue on `catui-plataform/CATui` (supports optional message IDs / links for screenshots and extra comments).
- `/release` - format the latest CATui release into the official Discord release announcement template.
- `/admin-add` - add an administrator to the bot (Owner only).
- `/admin-remove` - remove an administrator from the bot (Owner only).
- `/admins` - list current bot administrators.
