# Core Updates

## Sources

Each core in the store repository has `store/cores/<id>/source/`:

- `upstream.json`: upstream repository, baseline commit, and integration files.
- `integration.patch`: patches applied on top of the baseline, including new files.
- `integration/`: CATui adapter code for reading and comparison.
- `build.json`: compiler flags, build targets, and Windows package layout.
- `LICENSE`: original upstream project license.

Filtered source archives preserve the supplied baseline; local build artifacts, games, firmware, and decryption keys remain outside the store repository. The runner fetches the approved revision and reapplies the integration patch. The official `eden-emulator/mirror` provides Eden lookups when Forgejo API blocks access; builds remain locked to exact commit SHAs rather than moving branch tips.

Configured cores track commits on their upstream branch. Checks compare the exact source base saved in the store with the latest branch SHA. A source archive and upstream base are required; an old binary build receipt is not required for source comparison. All seven configured cores use the same build, repair and publication workflow. PPSSPP, Flycast and Cemu can bootstrap their initial build recipes from `config/builds/`; publication saves the recipe alongside the source for later updates. Cemu tracks official Cemu commits and carries its Libretro port as an integration patch from the public fork revision recorded in `integrationOrigin`. The recovered source has a Windows build recipe and both validators; its existing DLL remains unverified against that source until a successful approved build publishes a receipt. Published releases and their AI summaries are optional context and never determine the build target or block a commit check. Configurations reside in `config/cores.json`; monitored cores are managed via `/cores-panel`.

## GitHub Actions

In `catui-bot`, configure:

**Repository variables**:

- `DISCORD_OWNER_ID`: the same human Discord user ID configured in Square Cloud.
- `CORE_RUNNER`: optional; defaults to `windows-2025-vs2026`.
- `AI_REPAIR_ENABLED`: start with `false`; enable after verifying baseline builds.
- `DEEPSEEK_MODEL`: optional; defaults to `deepseek-flash`.
- `CMAKE_PREFIX_PATH`, `CMAKE_TOOLCHAIN_FILE`, `Qt6_DIR`, `LLVM_DIR`: if dependencies are preinstalled on the runner. PCSX2 uses the pinned static dependency manifest described below.

**Repository secrets**:

- `APPROVAL_SIGNING_KEY`: the same signing key configured in Square Cloud.
- `DEEPSEEK_API_KEY`: required for integration review and optional automated repair.

Create the **`core-publish` environment**, restricted to the `main` branch, and add:

- `STORE_PUBLISH_TOKEN`: fine-grained PAT scoped to `catui-plataform/CATui`, with Contents: Read and write, and Metadata: Read. The existing runtime PAT can be used if it also has these permissions; copy its value to this environment secret.

## Windows Dependencies

PCSX2 uses the pinned vcpkg revision and static triplet in `config/dependencies.json`, with packages declared in `config/vcpkg/pcsx2/vcpkg.json`. Actions caches compiled dependencies. The final DLL must import only allowed Windows system libraries.

Each core retains its own compiler and dependency requirements. RPCS3 requires Qt and LLVM; shadPS4 uses clang-cl and Vulkan; Eden uses CPM dependencies. PPSSPP and Flycast build their existing upstream Libretro targets; Cemu uses its integrated `cemu_libretro` target and static vcpkg dependencies. Eden has passed the full hosted build and publication flow; this is not evidence that every other core has passed a hosted build. Existing dependency bundles must be pinned by SHA-256.

Use the **Check core build without publishing** workflow to exercise any core at its recorded catalog baseline. It reuses the production build worker, including repair and both validators, and has no publication job. Initial hosted builds must pass the same mandatory gates as subsequent updates; a successful build from another core cannot substitute for these checks.

## Mandatory Validation

The build verifies the Libretro ABI in an isolated process, but this alone does not guarantee that the emulator functions inside CATui. Publication requires two validation layers: **native** and **catui**.

Commands for enabled cores are tracked in `validation/checks.json`; `validation/commands.json` is an optional local override. The native probe checks initialization, options, rejected content and cleanup. The frontend probe exercises the same limited lifecycle through the actual CATui LibretroSession using the pinned public frontend DLL and Godot in `config/frontend.json`. Neither test runs a game. Benchmarks, FPS checks and screenshots are outside the current scope. Local overrides are ignored to avoid committing private paths.

Format for each core: a list of objects with `layer`, `command` (argument list, without shell execution), `timeout`, `log` (generated output log), and `success` (substring indicating successful probe). `{package}` in arguments is replaced with the newly compiled core directory. Each command must terminate on its own. Limits are 240 seconds per command and 900 seconds for the entire step. The log must be fresh and contain the expected marker.

CATui probes must run the Godot CLI with explicit log output and a test scene/script that calls `get_tree().quit()`. Report the exact paths exercised; a lifecycle-only probe must not claim gameplay validation. For shadPS4, preserve video, audio, and integration lifecycle tests; a successful `retro_api_version()` does not replace these tests. Never commit commercial ROMs, BIOS files, decryption keys, firmware, or game saves.

Without these validators, the builder **fails before publishing**. This is by design. Once dependencies and both probes pass on the runner, enable `CORE_UPDATES_ENABLED=true` in Square Cloud.

## Workflow and Error Handling

An update proposal identifies core, version, commit SHA, current store revision, and a 24-hour expiration window. Clicking approve or replying `yes` signs this plan. GitHub Actions verifies the cryptographic signature before executing any emulator code. Commit SHAs cannot be altered via chat.

Before building, a separate access check tests store write permission without changing files, and DeepSeek reviews the focused integration/API changes. The worker receives only the DeepSeek credential; source build subprocesses receive no credentials. The same worker repairs source conflicts and compilation failures, then validates the DLL ABI and native/CATui lifecycle. Source repair and runtime validation have separate attempt budgets; Python helper failures use the supervisor. Validators, approval and publishing controls cannot be modified by the model. Exhausted attempts stop publication and preserve diagnostic artifacts. The server can resume an unchanged, still-approved failed request after updated bot code reaches main, provided the store has not changed.

The publisher runs in an isolated final job that revalidates the plan, package, source snapshot, hash, and both test results. It replaces the package, manifest and source in the core folder, refreshes the catalog, and pushes to main. It creates no tags or releases and never uses force-push. Releases are reserved for CATui builds. If the store repository changed since proposal creation, a new comparison is required. Packages of 100 MiB or larger are rejected until multi-part distribution is supported.

The final Discord card describes the published commit update and build results. LLM repairs are recorded in the published build receipt. Running `/cores-check` can re-propose failed, skipped or stale updates for a fresh comparison. Workflow logs and diagnostic artifacts remain available in Actions for seven days.
