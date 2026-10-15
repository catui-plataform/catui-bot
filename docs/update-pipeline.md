# Core update pipeline

Implementation plan. Commit monitoring and approval are implemented. Dependency
setup, adaptation and runtime validation still need calibration before the first
PCSX2 publication. Benchmarks, FPS measurement, gameplay screenshots and GPU jobs
are outside the scope. PCSX2 publishes one self-contained `pcsx2_libretro.dll`.

## Responsibilities

- Square Cloud handles owner-only Discord control, signed approvals, persistent
  state, progress and final reports. It does not compile emulators.
- GitHub Actions prepares sources, compiles and runs compatibility checks on a
  standard Windows runner. A separate final job publishes verified artifacts.
- DeepSeek Flash helps adapt the existing integration. It cannot approve updates,
  execute commands, change validation rules or certify its own changes as working.

## Approved update

1. Freeze the exact store revision, upstream commit and integration patch. Keep
   the baseline and target in the receipt. Moving branches never silently change
   an already approved build.
2. Check the dependency recipe, runner, native/CATui validators and publisher
   configuration before spending model tokens or starting a long compile.
3. Fetch the approved upstream SHA and apply the saved integration using Git's
   three-way merge, excluding `AGENTS.md` and `CLAUDE.md` instruction files.
   If application aborts before producing source conflicts, retain the real Git
   error instead of spending model calls on unchanged source. Preserve Libretro ABI, options, embedded resources, input,
   audio, graphics callbacks, save states, reset and shutdown behavior. Existing
   integration changes must not disappear just to resolve a conflict.
4. Give Flash only conflicting functions, relevant upstream declarations and
   focused diagnostics. Use thinking disabled for focused repair requests. Request bounded structured patches
   within the allowed integration paths, never arbitrary commands or edits to
   workflows, dependencies, validators or publishing controls.
5. Compile using pinned tools and cached static Windows dependencies. Check PE
   architecture, Libretro exports and imports. Reject PCSX2 packages requiring
   additional non-system DLLs: this target must remain a single usable DLL.
6. Run bounded native and headless CATui compatibility probes against the newly
   compiled DLL. Check identity, options, load/init/error paths, cleanup and
   repeated lifecycle calls supported by the probe. Test unsupported or missing
   content gracefully; do not pretend that a rejected content load proves that
   gameplay works. Keep the actual logs and identify which paths were exercised.
7. If an integration or compiler error has enough evidence for repair, request a
   focused AI patch and repeat the build and all gates. Bound repair attempts;
   network, missing configuration and permission failures do not trigger blind
   code repair attempts. A failed or missing validator stops publication.
8. In the isolated publisher, verify approval, checksums, test evidence and the
   unchanged store head. Replace only the selected core package, manifest and
   source metadata, then regenerate the catalog and push to `main`. No store tags
   or releases. Keep the previous store commit/package hash for explicit rollback.

## Validation scope

Static review, successful compilation, native lifecycle checks and a CATui
frontend probe reduce integration risk. They cannot guarantee unchanged game
compatibility, video/audio correctness, performance or save-state compatibility
without exercising those behaviors. Reports must distinguish tested paths from
untested gameplay. An AI review or successful DLL load is not a guarantee that
an emulator update has no regressions.

No game images, BIOS, GPU service, FPS baseline or visual benchmark are required
for this limited compatibility flow. A future real-game test is a separate
feature and must not be silently claimed by these probes.

## Discord

Use one progress card, updated only when the workflow stage changes:
`Preparing`, `Integrating`, `Building`, `Checking`, `Publishing`.
Report concise decisions and changed integration areas, without private model
reasoning, raw compiler noise or secrets. Stage updates come from workflow state
and do not need model calls. Persist the run/stage across bot restarts.

The final card shows the published commit, concise changes, compatibility checks
that passed and a build-report link. On failure, identify the stage and state that
the current store package was retained. Do not show FPS, screenshots, a gameplay
validation claim or a redundant CATui footer.

## Token and runner costs

- Watching commits, comparing files, applying patches, tracking progress and
  running tests use no LLM calls.
- Cache release summaries by description/model/prompt version, with thinking
  disabled. Releases remain optional context, independent of commit monitoring.
- Cache integration analysis by baseline, target, patch and model/prompt version.
- Send only relevant hunks, API declarations and filtered errors, not the entire
  source tree or full build logs. Prefer small patches to whole-file rewrites.
- Keep stable prompt prefixes to benefit from provider caching. Do not resend
  previous reasoning. Record actual input, cache and reasoning/output usage.
- Bound repair attempts per update, without a daily request counter blocking
  normal checks. An optional final summary only receives verified results.
- Cache pinned dependencies and compiler outputs. Standard runner minutes are
  free for this public repository; keep artifact/cache retention within the
  included allowances. Never upload whole build directories or silently enable
  paid storage. No paid runner or GPU job is part of this design.

## Isolation and calibration

Validate signed owner approvals and exact SHAs, and keep one update in flight.
The build supervisor receives the DeepSeek key; emulator subprocesses receive a filtered environment with no API keys, signing keys or tokens. The supervisor can repair traceback-referenced build helper Python files, but validators, approval and publishing code are outside its editable paths. Source repairs use current-attempt logs and retain successful edits between rebuilds. No separate repair/retry/result jobs or bot redispatch loop duplicate this work. Only the
final publisher receives its scoped store credential. Pin actions and tools.

First rebuild the existing integration without publishing, then port and check
an approved candidate. Initial inspection found a real three-way conflict in
`pcsx2/SIO/Pad/PadDualshock2.cpp`. It must be resolved without dropping the CATui
controller integration. Dependencies, both probe layers and publishing setup
still need calibration; the first full core update has not succeeded yet.

## References

- [DeepSeek thinking controls](https://api-docs.deepseek.com/guides/thinking_mode/)
- [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
